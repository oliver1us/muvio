"""Metadata, download, transcription, and job routes."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.admission import admission
from app.blocking import run_sync
from app.config import admin_settings, public_settings, save_settings, settings
from app.engine import ffmpeg, ytdlp
from app.errors import AppError, fail
from app.queue.jobs import Job, queue
from app.schemas import DownloadRequest, SubtitleRequest, TranscribeRequest
from app.identity import principal, require_admin
from app.storage import LeasedFileResponse, storage

router = APIRouter(prefix="/api")


def signed_url(request: Request, job: Job, ext: str | None = None) -> str:
    """Create a short-lived, job-bound download link."""
    import hashlib
    import hmac
    expires = int(time.time()) + 900
    value = f"{job.id}:{ext or ''}:{expires}"
    token = hmac.new(request.app.state.signing_key, value.encode(), hashlib.sha256).hexdigest()
    path = f"/api/files/{job.id}" + (f"/{quote(ext, safe='')}" if ext else "")
    return f"{path}?expires={expires}&token={token}"


def get_job(job_id: str, request: Request) -> Job:
    """Find a job without accepting user-supplied filesystem paths."""
    job = queue.get(job_id, principal(request).owners)
    if not job:
        raise fail("file.not_found", "Job not found.", 404)
    return job


def submission(job_id: str) -> dict:
    return {"status": "local-processing", "job_id": job_id, "output": {"job_id": job_id}}


@asynccontextmanager
async def reserve(request: Request, payload: dict, *, charge=True):
    if not queue.healthy:
        raise AppError("capacity.exceeded", "State persistence failed. The administrator must restore storage and restart the server.", 503, headers={"Retry-After": "60"})
    job_id, replay = admission.reserve(principal(request), request.headers.get("Idempotency-Key"), payload, charge=charge)
    try:
        if not replay:
            with admission.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM reservations WHERE state IN ('inspecting','queued','running')").fetchone()[0]
            await run_sync(storage.check_capacity, max(0, count - 1))
        yield job_id, replay
    finally:
        if not replay:
            admission.release(job_id)


async def inspect(url: str) -> dict:
    with admission.inspection():
        return await run_sync(ytdlp.info, url)


def validate_duration(data: dict):
    if (data.get("duration") or 0) > settings.max_media_duration_seconds:
        raise AppError("capacity.exceeded", "This source exceeds the configured duration limit.", 413)


async def download_operation(job: Job, payload: DownloadRequest) -> None:
    """Execute media extraction and optional audio post-processing."""
    from app.engine.formats import audio_conversion
    folder = storage.folder(job)
    try:
        result = await run_sync(ytdlp.download, payload.url, folder, payload, lambda p, s, e, stage: queue.update(job, p, s, e, stage), lambda: job.cancelled)
        if job.cancelled:
            raise AppError("job.cancelled", "The job was cancelled.")
        conversion = audio_conversion(payload)
        if conversion:
            queue.update(job, 99, None, None, "Converting audio")
            original = result
            target = original.with_suffix("." + conversion[0])
            if target == original:
                target = original.with_name(original.stem + ".converted" + original.suffix)
            result = await ffmpeg.extract_audio(result, target, *conversion)
            if payload.audioFormat == "m4a" and payload.embedThumbnail:
                covers = [image for image in folder.iterdir() if image.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
                if covers:
                    result = await ffmpeg.attach_cover(result, covers[0], result.with_name(result.stem + ".cover.m4a"))
                    target.unlink(missing_ok=True)
                else:
                    job.warning = "cover.unavailable: The source did not provide cover art."
            final = original.with_suffix("." + conversion[0])
            result.replace(final)
            result = final
        job.output_path, job.filename = str(result), result.name
        if payload.downloadSubtitles:
            job.files = {p.name: str(p) for p in folder.iterdir() if p.suffix.lower() in {".srt", ".vtt"} and p.is_file()}
    except asyncio.CancelledError:
        job.cancelled = True
        raise


@router.get("/health")
async def health(request: Request) -> dict:
    """Report local dependency availability without network activity."""
    result = {"status": "ok", "version": "0.1.0"}
    if principal(request).admin:
        result.update(ffmpeg=ffmpeg.available(), yt_dlp=True, whisper=importlib.util.find_spec("faster_whisper") is not None, demucs=importlib.util.find_spec("demucs") is not None, models={"directory": str(settings.models_dir), "configured": settings.whisper_model})
    return result


@router.get("/session")
async def session(request: Request) -> dict:
    actor = principal(request)
    return {"identity": "anonymous", "administrator": actor.admin, "quota": admission.quota(actor), **public_settings()}


@router.get("/info")
async def info(url: str) -> dict:
    """Fetch normalized media metadata."""
    return await inspect(url)


@router.get("/subtitles")
async def subtitles(url: str) -> dict:
    """List all source subtitle variants."""
    return {"tracks": (await inspect(url))["subtitles"]}


@router.post("/download")
async def download(payload: DownloadRequest, request: Request) -> dict:
    """Validate media and enqueue a download, or offer playlist items."""
    async with reserve(request, {"kind": "download", **payload.model_dump()}) as (job_id, replay):
        if replay:
            return submission(job_id)
        data = await inspect(payload.url)
        if data["entries"]:
            return {"status": "picker", "picker": data["entries"]}
        if data["is_live"]:
            raise AppError("media.live", "Live streams are not supported.")
        validate_duration(data)
        if payload.downloadMode == "auto" and not data["videoStreams"] and data["audioStreams"]:
            payload = payload.model_copy(update={"downloadMode": "audio"})
        if payload.downloadMode == "audio" and not data["audioStreams"]:
            raise AppError("format.unavailable", "This source does not contain an audio track.")
        if payload.embedSubtitles or payload.downloadSubtitles:
            if not any(track["lang"] == payload.subtitleLang and track["is_automatic"] == payload.subtitleAutomatic for track in data["subtitles"]):
                raise AppError("format.unavailable", "The selected subtitle language and track type are not available.")
        from app.engine.formats import build_plan
        plan = build_plan(payload, data["formats"])
        if plan.audio_language:
            payload = payload.model_copy(update={"audioLanguage": plan.audio_language})
        job = queue.submit("download", payload.url, lambda j: download_operation(j, payload), owner=principal(request).owner, job_id=job_id)
        return submission(job.id)


@router.post("/subtitle-download")
async def subtitle_download(payload: SubtitleRequest, request: Request) -> dict:
    """Queue a specific manual or automatic subtitle variant."""
    async with reserve(request, {"kind": "subtitle", **payload.model_dump()}) as (job_id, replay):
        if replay:
            return submission(job_id)
        data = await inspect(payload.url)
        validate_duration(data)
        if not any(track["lang"] == payload.lang and track["is_automatic"] == payload.automatic for track in data["subtitles"]):
            raise AppError("format.unavailable", "This subtitle track is not available. Choose another language or source track.")
        async def operation(job: Job) -> None:
            path = await run_sync(ytdlp.download_subtitle, payload.url, storage.folder(job), payload.lang, payload.automatic, payload.format, lambda: job.cancelled)
            job.output_path, job.filename = str(path), path.name
        job = queue.submit("subtitle", payload.url, operation, owner=principal(request).owner, job_id=job_id)
        return submission(job.id)


async def transcription_operation(job: Job, payload: TranscribeRequest, uploaded: Path | None) -> None:
    """Download/extract audio, transcribe, optionally separate, and export."""
    from app.transcription import engine, separate, studio, cleanup
    folder = storage.folder(job)
    folder.mkdir(parents=True, exist_ok=True)
    source = uploaded
    if payload.url:
        request = DownloadRequest(url=payload.url, downloadMode="audio", audioFormat="best", embedThumbnail=False, embedMetadata=False)
        source = await run_sync(ytdlp.download, payload.url, folder, request, lambda p, s, e, stage: queue.update(job, p * 0.2, s, e, stage), lambda: job.cancelled)
    if source is None:
        raise AppError("url.invalid", "Provide a URL or an audio file.")
    try:
        if job.cancelled:
            raise AppError("job.cancelled", "The job was cancelled.")
        title = payload.title or source.stem
        prompt = f"{title}. {payload.artist}." if payload.mode == "studio" else ""
        audio = source
        if payload.mode == "studio":
            queue.update(job, 25, None, None, "Preprocessing audio")
            audio = await ffmpeg.preprocess(source, folder / "prepared.wav", payload.vocalFocus)
        queue.update(job, 40, None, None, "Transcribing original audio")
        original = await run_sync(engine.transcribe, audio, payload.language, payload.mode == "studio", prompt)
        if job.cancelled:
            raise AppError("job.cancelled", "The job was cancelled.")
        rows = original["segments"]
        if payload.mode == "studio" and payload.separateVocals:
            queue.update(job, 60, None, None, "Separating vocals")
            try:
                stem = await run_sync(separate.separate, source, folder / "stems")
                if stem:
                    if job.cancelled:
                        raise AppError("job.cancelled", "The job was cancelled.")
                    queue.update(job, 75, None, None, "Transcribing isolated vocals")
                    second = await run_sync(engine.transcribe, stem, payload.language, True, prompt)
                    rows = studio.merge(rows, second["segments"])
                else:
                    job.warning = "demucs.unavailable: Install and enable Demucs to separate vocals. Original audio was used."
            except AppError as exc:
                if exc.code == "job.cancelled":
                    raise
                job.warning = f"{exc.code}: {exc} Original audio was used."
        if payload.aiCleanup:
            queue.update(job, 90, None, None, "Correcting transcript")
            corrected, warning = await cleanup.cleanup([r["text"] for r in rows], title, payload.artist)
            for row, text in zip(rows, corrected):
                row["text"] = text
            job.warning = warning or job.warning
        if job.cancelled:
            raise AppError("job.cancelled", "The job was cancelled.")
        queue.update(job, 95, None, None, "Exporting transcript")
        paths = studio.export(original | {"segments": rows}, folder, title)
        job.files = {ext: str(path) for ext, path in paths.items()}
        job.output_path, job.filename = str(paths["srt"]), paths["srt"].name
    finally:
        if uploaded:
            uploaded.unlink(missing_ok=True)


@router.post("/transcribe")
async def transcribe(request: Request, file: UploadFile | None = File(None), options: str | None = Form(None)) -> dict:
    """Submit a URL as JSON or an audio upload as multipart form data."""
    actor = require_admin(request)
    uploaded = None
    if file:
        try:
            payload = TranscribeRequest.model_validate_json(options or "{}")
        except ValueError as exc:
            raise AppError("url.invalid", "Invalid transcription options.") from exc
        if not file.content_type or not (file.content_type.startswith("audio/") or file.content_type.startswith("video/")):
            raise AppError("format.unavailable", "Upload an audio or video file.")
        if payload.url:
            raise AppError("url.invalid", "Choose a URL or an upload, not both.", 422)
        # Hash the already bounded/spooled upload without loading it into RAM.
        # Idempotent multipart retries compare actual content, not just filenames.
        digest, size = hashlib.sha256(), 0
        with admission.inspection():
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > min(settings.max_upload_mb, settings.max_job_mb) * 1024 * 1024:
                    raise AppError("capacity.exceeded", "The upload exceeds the configured size limit.", 413)
                digest.update(chunk)
            await file.seek(0)
        fingerprint = {"upload_sha256": digest.hexdigest(), "filename": file.filename}
    else:
        try:
            payload = TranscribeRequest.model_validate(await request.json())
        except ValueError as exc:
            raise AppError("url.invalid", "Invalid transcription options.", 422) from exc
        if not payload.url:
            raise AppError("url.invalid", "Provide a URL or an audio file.")
        fingerprint = {}
    async with reserve(request, {"kind": "transcribe", **payload.model_dump(), **fingerprint}, charge=False) as (job_id, replay):
        if replay:
            return submission(job_id)
        if payload.url:
            data = await inspect(payload.url)
            validate_duration(data)
            if data.get("is_live") or data.get("entries"):
                raise AppError("format.unavailable", "Choose a single, non-live source for transcription.")
        job = queue.prepare("transcribe", payload.url or "Local file", owner=actor.owner, job_id=job_id)
        try:
            if file:
                suffix = Path(file.filename or "audio.wav").suffix
                suffix = suffix if suffix[1:].isalnum() and len(suffix) <= 12 else ".audio"
                uploaded = storage.folder(job) / ("upload" + suffix)
                size = 0
                with uploaded.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        if job.cancelled:
                            raise AppError("job.cancelled", "The upload was cancelled.")
                        size += len(chunk)
                        if size > min(settings.max_upload_mb, settings.max_job_mb) * 1024 * 1024:
                            raise AppError("capacity.exceeded", "The upload exceeds the configured size limit.", 413)
                        await run_sync(output.write, chunk)
                duration = (await ffmpeg.probe(uploaded)).get("format", {}).get("duration")
                validate_duration({"duration": float(duration or 0)})
            queue.submit("transcribe", job.url, lambda j: transcription_operation(j, payload, uploaded), owner=actor.owner, prepared=job)
        except BaseException:
            await queue.abort(job)
            raise
        finally:
            if file:
                await file.close()
        return submission(job.id)


@router.get("/jobs")
async def jobs(request: Request, limit: int = Query(30, ge=1, le=100), cursor: str | None = Query(None, max_length=512)) -> dict:
    """Return current and historical jobs."""
    position = None
    if cursor:
        try:
            position = json.loads(base64.urlsafe_b64decode(cursor).decode())
            if not isinstance(position, list) or len(position) != 2 or not all(isinstance(v, str) for v in position):
                raise ValueError("Invalid cursor")
        except (ValueError, UnicodeError) as exc:
            raise AppError("url.invalid", "Invalid history cursor.", 422) from exc
    rows = queue.page(principal(request).owners, limit + 1, position)
    next_cursor = base64.urlsafe_b64encode(json.dumps([rows[limit - 1].created_at, rows[limit - 1].id]).encode()).decode() if len(rows) > limit else None
    return {"items": [j.public() for j in rows[:limit]], "next_cursor": next_cursor}


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, request: Request) -> dict:
    """Return a job and fresh signed links when complete."""
    job = get_job(job_id, request)
    result = job.public()
    result["source_url"] = result.pop("url")
    if job.status == "done" and result["availability"] == "available":
        result["url"] = signed_url(request, job)
        result["downloads"] = {ext: signed_url(request, job, ext) for ext in job.files}
    return result


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, request: Request) -> dict:
    """Cancel queued work or signal a running task."""
    job = get_job(job_id, request)
    if job.status in {"done", "error", "cancelled"}:
        raise AppError("job.cancelled", "This job is already finished.")
    await queue.cancel(job)
    return job.public()


@router.api_route("/files/{job_id}", methods=["GET", "HEAD"])
@router.api_route("/files/{job_id}/{ext}", methods=["GET", "HEAD"])
async def file(job_id: str, request: Request, expires: int, token: str, ext: str | None = None):
    """Serve only job-owned files with a valid short-lived HMAC link."""
    import hashlib
    import hmac
    value = f"{job_id}:{ext or ''}:{expires}"
    expected = hmac.new(request.app.state.signing_key, value.encode(), hashlib.sha256).hexdigest()
    if expires < time.time() or expires > time.time() + 901 or not hmac.compare_digest(token, expected):
        raise fail("file.not_found", "This download link has expired or is invalid.", 403)
    # A valid signature is a bearer capability; no browser cookie is required.
    # No unsigned lookup may reveal whether the job exists.
    job = None
    for history in queue.histories:
        with history.connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row:
            job = history.decode(row[0])
            break
    if job is None:
        raise fail("file.not_found", "The requested file is not ready.", 404)
    value = job.files.get(ext) if ext else job.output_path
    try:
        path = storage.path(job, value) if value else None
    except ValueError:
        path = None
    if job.status != "done" or path is None:
        raise fail("file.not_found", "The requested file is not ready.", 404)
    response = LeasedFileResponse(job, path)
    response.headers["Cache-Control"] = "private, no-store"
    storage.acquire(job)
    return response


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Broadcast progress with heartbeat and per-connection bounded buffers."""
    actor = principal(request)
    if len(queue.subscribers) >= settings.max_event_subscribers or sum(owner == actor.owner for owner in queue.subscribers.values()) >= 3:
        raise AppError("capacity.exceeded", "Too many event connections.", 429)
    subscriber: asyncio.Queue = asyncio.Queue(maxsize=100)
    queue.subscribers[subscriber] = actor.owner
    async def stream():
        try:
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(subscriber.get(), 15)
                    yield "event: progress\ndata: " + json.dumps(data) + "\n\n"
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            queue.subscribers.pop(subscriber, None)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    """Get non-secret configuration."""
    require_admin(request)
    return admin_settings()


@router.put("/settings")
async def put_settings(values: dict, request: Request) -> dict:
    """Persist validated preferences and optional provider credentials."""
    require_admin(request)
    try:
        return save_settings(values)
    except ValueError as exc:
        raise AppError("url.invalid", "Invalid settings values.", 422) from exc


@router.post("/models/whisper")
async def model_download(request: Request) -> dict:
    """Explicit opt-in to downloading the configured model from Hugging Face."""
    actor = require_admin(request)
    from app.transcription.engine import download_model
    async def operation(job: Job) -> None:
        job.stage = "Downloading Whisper model"
        job.filename = await run_sync(download_model)
    async with reserve(request, {"kind": "model", "model": "whisper"}, charge=False) as (job_id, replay):
        if not replay:
            queue.submit("model", "Hugging Face", operation, owner=actor.owner, job_id=job_id)
        return submission(job_id)


@router.post("/models/demucs")
async def demucs_model_download(request: Request) -> dict:
    """Explicit opt-in to downloading Demucs vocal-separation weights."""
    actor = require_admin(request)
    from app.transcription.separate import download_model
    async def operation(job: Job) -> None:
        job.stage = "Downloading Demucs model"
        job.filename = await run_sync(download_model)
    async with reserve(request, {"kind": "model", "model": "demucs"}, charge=False) as (job_id, replay):
        if not replay:
            queue.submit("model", "Demucs", operation, owner=actor.owner, job_id=job_id)
        return submission(job_id)

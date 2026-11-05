"""Metadata, download, transcription, and job routes."""
import asyncio
import importlib.util
import json
import secrets
import shutil
import time
from pathlib import Path

import anyio
import yt_dlp
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.config import public_settings, save_settings, settings
from app.engine import ffmpeg, ytdlp
from app.errors import AppError, error_body, fail
from app.queue.jobs import Job, queue
from app.schemas import DownloadRequest, SubtitleRequest, TranscribeRequest

router = APIRouter(prefix="/api")


def signed_url(request: Request, job: Job, ext: str | None = None) -> str:
    """Create a short-lived, job-bound download link."""
    import hashlib
    import hmac
    expires = int(time.time()) + 900
    value = f"{job.id}:{ext or ''}:{expires}"
    token = hmac.new(request.app.state.signing_key, value.encode(), hashlib.sha256).hexdigest()
    path = f"/api/files/{job.id}" + (f"/{ext}" if ext else "")
    return f"{path}?expires={expires}&token={token}"


def get_job(job_id: str) -> Job:
    """Find a job without accepting user-supplied filesystem paths."""
    job = queue.jobs.get(job_id)
    if not job:
        raise fail("file.not_found", "Job not found.", 404)
    return job


async def download_operation(job: Job, payload: DownloadRequest) -> None:
    """Execute media extraction and optional audio post-processing."""
    from app.engine.formats import build_plan
    folder = settings.output_dir / job.id
    try:
        result = await anyio.to_thread.run_sync(ytdlp.download, payload.url, folder, payload, lambda p, s, e, stage: queue.update(job, p, s, e, stage), lambda: job.cancelled)
        if job.cancelled:
            raise AppError("job.cancelled", "The job was cancelled.")
        conversion = build_plan(payload).audio_conversion
        if conversion:
            queue.update(job, 99, None, None, "Converting audio")
            result = await ffmpeg.extract_audio(result, result.with_suffix("." + conversion[0]), *conversion)
        job.output_path, job.filename = str(result), result.name
    except asyncio.CancelledError:
        job.cancelled = True
        raise


@router.get("/health")
async def health() -> dict:
    """Report local dependency availability without network activity."""
    return {"status": "ok", "version": "0.1.0", "ffmpeg": ffmpeg.available(), "yt_dlp": True, "whisper": importlib.util.find_spec("faster_whisper") is not None, "demucs": importlib.util.find_spec("demucs") is not None, "models": {"directory": str(settings.models_dir), "configured": settings.whisper_model}}


@router.get("/info")
async def info(url: str) -> dict:
    """Fetch normalized media metadata."""
    return await anyio.to_thread.run_sync(ytdlp.info, url)


@router.get("/subtitles")
async def subtitles(url: str) -> dict:
    """List all source subtitle variants."""
    return {"tracks": (await anyio.to_thread.run_sync(ytdlp.info, url))["subtitles"]}


@router.post("/download")
async def download(payload: DownloadRequest, request: Request) -> dict:
    """Validate media and enqueue a download, or offer playlist items."""
    data = await anyio.to_thread.run_sync(ytdlp.info, payload.url)
    if data["entries"]:
        return {"status": "picker", "picker": data["entries"]}
    if data["is_live"]:
        raise AppError("media.live", "Live streams are not supported.")
    if payload.downloadMode != "audio" and data["formats"]:
        from app.engine.formats import build_plan
        codec = {"h264": "avc1", "vp9": "vp9", "av1": "av01"}[payload.videoCodec]
        candidates = [f for f in data["videoStreams"] if (f.get("vcodec") or "").startswith(codec) and (payload.videoQuality == "max" or (f.get("height") or 0) <= int(payload.videoQuality))]
        if not candidates:
            raise AppError("format.unavailable", "The selected codec and quality are not available for this media.")
    job = queue.submit("download", payload.url, lambda j: download_operation(j, payload))
    return {"status": "local-processing", "url": signed_url(request, job), "filename": None, "output": {"job_id": job.id}}


@router.post("/subtitle-download")
async def subtitle_download(payload: SubtitleRequest, request: Request) -> dict:
    """Queue a specific manual or automatic subtitle variant."""
    async def operation(job: Job) -> None:
        path = await anyio.to_thread.run_sync(ytdlp.download_subtitle, payload.url, settings.output_dir / job.id, payload.lang, payload.automatic, payload.format)
        job.output_path, job.filename = str(path), path.name
    job = queue.submit("subtitle", payload.url, operation)
    return {"status": "local-processing", "url": signed_url(request, job), "output": {"job_id": job.id}}


async def transcription_operation(job: Job, payload: TranscribeRequest, uploaded: Path | None) -> None:
    """Download/extract audio, transcribe, optionally separate, and export."""
    from app.transcription import engine, separate, studio, cleanup
    folder = settings.output_dir / job.id
    folder.mkdir(parents=True, exist_ok=True)
    source = uploaded
    if payload.url:
        request = DownloadRequest(url=payload.url, downloadMode="audio", audioFormat="best", embedThumbnail=False, embedMetadata=False)
        source = await anyio.to_thread.run_sync(ytdlp.download, payload.url, folder, request, lambda p, s, e, stage: queue.update(job, p * 0.2, s, e, stage), lambda: job.cancelled)
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
        original = await anyio.to_thread.run_sync(engine.transcribe, audio, payload.language, payload.mode == "studio", prompt)
        if job.cancelled:
            raise AppError("job.cancelled", "The job was cancelled.")
        rows = original["segments"]
        if payload.mode == "studio" and payload.separateVocals:
            queue.update(job, 60, None, None, "Separating vocals")
            try:
                stem = await anyio.to_thread.run_sync(separate.separate, source, settings.output_dir.parent / "stems")
                if stem:
                    if job.cancelled:
                        raise AppError("job.cancelled", "The job was cancelled.")
                    queue.update(job, 75, None, None, "Transcribing isolated vocals")
                    second = await anyio.to_thread.run_sync(engine.transcribe, stem, payload.language, True, prompt)
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
    uploaded = None
    if file:
        try:
            payload = TranscribeRequest.model_validate_json(options or "{}")
        except ValueError as exc:
            raise AppError("url.invalid", "Invalid transcription options.") from exc
        if not file.content_type or not (file.content_type.startswith("audio/") or file.content_type.startswith("video/")):
            raise AppError("format.unavailable", "Upload an audio or video file.")
        folder = settings.output_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        uploaded = folder / (secrets.token_hex(16) + Path(file.filename or "audio.wav").suffix[:12])
        size = 0
        try:
            with uploaded.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_mb * 1024 * 1024:
                        raise AppError("format.unavailable", "The upload exceeds the configured size limit.", 413)
                    await anyio.to_thread.run_sync(output.write, chunk)
        except Exception:
            uploaded.unlink(missing_ok=True)
            raise
    else:
        payload = TranscribeRequest.model_validate(await request.json())
        if not payload.url:
            raise AppError("url.invalid", "Provide a URL or an audio file.")
        await anyio.to_thread.run_sync(ytdlp.validate_url, payload.url)
    job = queue.submit("transcribe", payload.url or "Local file", lambda j: transcription_operation(j, payload, uploaded))
    return {"status": "local-processing", "job_id": job.id, "output": {"job_id": job.id}, "stages": ["preprocess", "separate", "transcribe", "merge", "export"]}


@router.get("/jobs")
async def jobs() -> list[dict]:
    """Return current and historical jobs."""
    return [j.public() for j in sorted(queue.jobs.values(), key=lambda x: x.created_at, reverse=True)]


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, request: Request) -> dict:
    """Return a job and fresh signed links when complete."""
    job = get_job(job_id)
    result = job.public()
    if job.status == "done":
        result["url"] = signed_url(request, job)
        result["downloads"] = {ext: signed_url(request, job, ext) for ext in job.files}
    return result


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict:
    """Cancel queued work or signal a running task."""
    job = get_job(job_id)
    if job.status in {"done", "error", "cancelled"}:
        raise AppError("job.cancelled", "This job is already finished.")
    queue.cancel(job)
    return job.public()


@router.get("/files/{job_id}")
@router.get("/files/{job_id}/{ext}")
async def file(job_id: str, request: Request, expires: int, token: str, ext: str | None = None) -> FileResponse:
    """Serve only job-owned files with a valid short-lived HMAC link."""
    import hashlib
    import hmac
    job = get_job(job_id)
    value = f"{job.id}:{ext or ''}:{expires}"
    expected = hmac.new(request.app.state.signing_key, value.encode(), hashlib.sha256).hexdigest()
    if expires < time.time() or expires > time.time() + 901 or not hmac.compare_digest(token, expected):
        raise fail("file.not_found", "This download link has expired or is invalid.", 403)
    path = Path(job.files.get(ext) if ext else job.output_path) if (job.files.get(ext) if ext else job.output_path) else None
    if job.status != "done" or path is None or not path.resolve().is_relative_to((settings.output_dir / job.id).resolve()) or not path.is_file():
        raise fail("file.not_found", "The requested file is not ready.", 404)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Broadcast progress with heartbeat and per-connection bounded buffers."""
    async def stream():
        subscriber: asyncio.Queue = asyncio.Queue(maxsize=100)
        queue.subscribers.add(subscriber)
        try:
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(subscriber.get(), 15)
                    yield "event: progress\ndata: " + json.dumps(data) + "\n\n"
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            queue.subscribers.discard(subscriber)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/settings")
async def get_settings() -> dict:
    """Get non-secret configuration."""
    return public_settings()


@router.put("/settings")
async def put_settings(values: dict) -> dict:
    """Persist validated preferences and optional provider credentials."""
    return save_settings(values)


@router.post("/models/whisper")
async def model_download() -> dict:
    """Explicit opt-in to downloading the configured model from Hugging Face."""
    from app.transcription.engine import download_model
    async def operation(job: Job) -> None:
        job.stage = "Downloading Whisper model"
        job.filename = await anyio.to_thread.run_sync(download_model)
    job = queue.submit("model", "Hugging Face", operation)
    return {"status": "local-processing", "output": {"job_id": job.id}}

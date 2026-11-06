"""Synchronous yt-dlp library adapter; callers execute it in worker threads."""
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import yt_dlp

from app.config import settings
from app.engine.audio import audio_tracks
from app.engine.formats import build_plan, video_options
from app.engine.naming import filename, unique
from app.errors import AppError
from app.schemas import DownloadRequest

_cache: dict[str, tuple[float, dict]] = {}


def validate_url(url: str) -> None:
    """Reject local/private addresses and unsupported URL schemes."""
    import ipaddress
    import socket

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise AppError("url.invalid", "Enter a public HTTP or HTTPS media URL.")
    try:
        for result in socket.getaddrinfo(parts.hostname, None):
            if not ipaddress.ip_address(result[4][0]).is_global:
                raise AppError("url.invalid", "Local and private network URLs are not allowed.")
    except OSError as exc:
        raise AppError("url.invalid", "The URL hostname could not be resolved.") from exc


def mapped_error(exc: Exception) -> AppError:
    """Convert extraction failures to readable public errors."""
    message = str(exc).lower()
    for needle, code, friendly in [
        ("unsupported url", "url.unsupported", "This URL is not supported by yt-dlp."),
        ("private", "media.private", "This media is private."),
        ("unavailable", "media.unavailable", "This media is unavailable."),
        ("live event", "media.live", "This live event has not started."),
        ("requested format", "format.unavailable", "The requested format is unavailable."),
        ("not a bot", "media.bot_check", "The site requires a bot check."),
        ("timed out", "net.timeout", "The media site timed out."),
    ]:
        if needle in message:
            return AppError(code, friendly)
    return AppError("media.unavailable", "The site could not provide this media: " + str(exc)[:240])


def options() -> dict:
    """Disable global configuration and yt-dlp console output."""
    return {"ignore_config": True, "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "cachedir": False, "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
            "playlistend": 100}


def tracks(info: dict) -> list[dict]:
    """List manual and auto-generated tracks separately."""
    result = []
    for key, automatic in (("subtitles", False), ("automatic_captions", True)):
        for lang, items in (info.get(key) or {}).items():
            if "live_chat" in lang:
                continue
            for item in items:
                if "live_chat" in item.get("name", "").lower():
                    continue
                result.append({"lang": lang, "ext": item.get("ext", "vtt"), "name": item.get("name") or lang, "is_automatic": automatic, "is_translated": automatic and "translated" in item.get("name", "").lower()})
    return result


def info(url: str) -> dict:
    """Fetch and normalize public metadata, cached for five minutes."""
    validate_url(url)
    if url in _cache and time.time() - _cache[url][0] < 300:
        return _cache[url][1]
    try:
        with yt_dlp.YoutubeDL(options() | {"skip_download": True, "extract_flat": False}) as ydl:
            raw = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise mapped_error(exc) from exc
    if not raw:
        raise AppError("media.unavailable", "No media was found at this URL.")
    if raw.get("entries"):
        entries = [{"id": e.get("id"), "title": e.get("title"), "url": e.get("webpage_url") or e.get("url")} for e in raw["entries"] if e]
    else:
        entries = []
    formats = [{"format_id": f.get("format_id"), "ext": f.get("ext"), "resolution": f.get("resolution"), "height": f.get("height"), "fps": f.get("fps"), "vcodec": f.get("vcodec"), "acodec": f.get("acodec"), "filesize": f.get("filesize"), "filesize_approx": f.get("filesize_approx"), "abr": f.get("abr"), "asr": f.get("asr"), "vbr": f.get("vbr"), "tbr": f.get("tbr"), "language": f.get("language"), "language_preference": f.get("language_preference"), "note": f.get("format_note"), "has_audio": f.get("acodec") not in (None, "none"), "has_video": f.get("vcodec") not in (None, "none")} for f in raw.get("formats") or []]
    data = {k: raw.get(k) for k in ("title", "uploader", "duration", "thumbnail", "webpage_url", "extractor", "is_live", "id")}
    data.update(formats=formats, subtitles=tracks(raw), entries=entries, audioStreams=[f for f in formats if f["has_audio"]], videoStreams=[f for f in formats if f["has_video"]])
    data["audioTracks"] = audio_tracks(formats)
    data["defaultAudioLanguage"] = data["audioTracks"][0]["language"] if data["audioTracks"] else None
    combinations = video_options(formats)
    data["videoOptions"] = combinations
    for key, value in list(_cache.items()):
        if time.time() - value[0] >= 300:
            _cache.pop(key, None)
    while len(_cache) >= 128:
        _cache.pop(next(iter(_cache)), None)
    _cache[url] = (time.time(), data)
    return data


def download(url: str, folder: Path, request: DownloadRequest, progress: Callable[[float, float | None, float | None, str], None], cancelled: Callable[[], bool]) -> Path:
    """Download to a job-owned folder; capture the actual postprocessed file."""
    validate_url(url)
    folder.mkdir(parents=True, exist_ok=True)
    plan = build_plan(request, info(url)["formats"])
    def hook(data: dict) -> None:
        if cancelled():
            raise AppError("job.cancelled", "The job was cancelled.")
        if data.get("downloaded_bytes", 0) > settings.max_job_mb * 1024 * 1024:
            raise AppError("capacity.exceeded", "The download exceeded the configured size limit.", 413)
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            progress(100 * data.get("downloaded_bytes", 0) / total if total else 0, data.get("speed"), data.get("eta"), "Downloading")
    opts = options() | {"format": plan.selector, "outtmpl": str(folder / "%(title).150s [%(id)s].%(ext)s"), "paths": {"home": str(folder), "temp": str(folder)}, "max_filesize": settings.max_job_mb * 1024 * 1024, "progress_hooks": [hook], "postprocessor_hooks": [hook], "postprocessors": list(plan.postprocessors), "writethumbnail": request.embedThumbnail and request.downloadMode == "audio" and request.audioFormat not in {"best", "wav", "flac", "opus", "ogg"}, "overwrites": False}
    if plan.container:
        opts["merge_output_format"] = plan.container
        opts["postprocessors"].append({"key": "FFmpegVideoRemuxer", "preferedformat": plan.container})
    if opts["writethumbnail"]:
        if request.audioFormat != "m4a":
            opts["postprocessors"].append({"key": "EmbedThumbnail"})
    if request.embedMetadata:
        opts["postprocessors"].append({"key": "FFmpegMetadata"})
    if request.downloadSubtitles or request.embedSubtitles:
        if not request.subtitleLang:
            raise AppError("format.unavailable", "Choose a subtitle language first.")
        opts.update(writesubtitles=not request.subtitleAutomatic, writeautomaticsub=request.subtitleAutomatic, subtitleslangs=[request.subtitleLang], subtitlesformat="vtt/srt/best")
        if request.embedSubtitles:
            opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})
            opts["embedsubtitles"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        if cancelled():
            raise AppError("job.cancelled", "The job was cancelled.") from exc
        raise mapped_error(exc) from exc
    files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() not in {".part", ".ytdl", ".jpg", ".png", ".webp", ".srt", ".vtt"}]
    if not files:
        raise AppError("file.not_found", "No output file was produced.")
    result = max(files, key=lambda p: p.stat().st_mtime)
    metadata = _cache.get(url, (0, {}))[1]
    if metadata:
        target = folder / filename(request.filenameStyle, metadata, result.suffix)
        if result != target:
            target = unique(target)
            result.rename(target)
            result = target
    return result


def download_subtitle(url: str, folder: Path, lang: str, automatic: bool, fmt: str, cancelled=lambda: False) -> Path:
    """Download one subtitle track through yt-dlp."""
    validate_url(url)
    folder.mkdir(parents=True, exist_ok=True)
    def hook(_):
        if cancelled():
            raise AppError("job.cancelled", "The job was cancelled.")
    hook(None)
    opts = options() | {"skip_download": True, "writesubtitles": not automatic, "writeautomaticsub": automatic, "subtitleslangs": [lang], "subtitlesformat": f"{fmt}/vtt/srt/best", "outtmpl": str(folder / "%(title).150s.%(ext)s"), "paths": {"home": str(folder), "temp": str(folder)}, "progress_hooks": [hook]}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        hook(None)
        raise mapped_error(exc) from exc
    hook(None)
    files = list(folder.glob("*.vtt")) + list(folder.glob("*.srt"))
    if not files:
        raise AppError("format.unavailable", "This subtitle track is unavailable.")
    source = files[0]
    if fmt == "srt" and source.suffix == ".vtt":
        from app.engine.subtitles import vtt_to_srt
        target = source.with_suffix(".srt")
        target.write_text(vtt_to_srt(source.read_text(encoding="utf-8")), encoding="utf-8")
        return target
    if fmt == "vtt" and source.suffix == ".srt":
        from app.engine.subtitles import srt_to_vtt
        target = source.with_suffix(".vtt")
        target.write_text(srt_to_vtt(source.read_text(encoding="utf-8")), encoding="utf-8")
        return target
    return source

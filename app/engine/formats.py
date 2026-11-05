"""yt-dlp format selection without video re-encoding."""
from dataclasses import dataclass, field

from app.engine.audio import audio_rank, audio_tracks, choose_audio, is_original, language, resolve_language
from app.errors import AppError
from app.schemas import DownloadRequest


VIDEO_PREFERENCES = [
    ("h264", "mp4"), ("vp9", "webm"), ("av1", "webm"),
    ("h264", "mkv"), ("vp9", "mp4"), ("av1", "mp4"),
    ("vp9", "mkv"), ("av1", "mkv"),
]


def video_codec(value: str | None) -> str | None:
    """Normalize the codec names exposed by different extractors."""
    for name, prefixes in {"h264": ("avc1", "h264"), "vp9": ("vp9", "vp09"), "av1": ("av01", "av1")}.items():
        if (value or "").startswith(prefixes):
            return name
    return None


def compatible_audio(codec: str | None, container: str) -> bool:
    """Keep automatic MP4 broadly playable and WebM standards-compliant."""
    if codec in (None, "none"):
        return False
    if container == "mp4":
        return codec.startswith(("mp4a", "aac"))
    if container == "webm":
        return codec.startswith(("opus", "vorbis"))
    return True


def video_options(formats: list[dict], audio_language: str | None = None) -> list[dict]:
    """Build language-specific options before applying container preferences."""
    languages = [audio_language] if audio_language else [track["language"] for track in audio_tracks(formats)] or [None]
    return [option for code in languages for option in _video_options(formats, code)]


def _video_options(formats: list[dict], audio_language: str | None) -> list[dict]:
    """Return only combinations that can be downloaded without video recoding."""
    audio = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none") and f.get("format_id") and language(f) == audio_language]
    source_has_audio = any(f.get("acodec") not in (None, "none") for f in formats)
    options = []
    for stream in formats:
        codec = video_codec(stream.get("vcodec"))
        if not codec or not stream.get("format_id"):
            continue
        has_audio = stream.get("acodec") not in (None, "none")
        if has_audio and language(stream) != audio_language:
            continue
        for container in ("mp4", "webm", "mkv"):
            if container == "webm" and codec == "h264":
                continue
            if has_audio:
                if not compatible_audio(stream["acodec"], container):
                    continue
                audio_id = None
                selected_audio = stream
            else:
                candidates = [f for f in audio if compatible_audio(f.get("acodec"), container)]
                if not candidates:
                    if source_has_audio:
                        continue
                    audio_id = None  # A genuinely silent source still has a valid video.
                    selected_audio = None
                else:
                    best = max(candidates, key=audio_rank)
                    audio_id = str(best["format_id"])
                    selected_audio = best
            options.append({
                "codec": codec, "container": container, "height": stream.get("height") or 0,
                "video_id": str(stream["format_id"]), "audio_id": audio_id,
                "fps": stream.get("fps") or 0, "tbr": stream.get("tbr") or 0,
                "audio_language": audio_language, "audio_original": bool(selected_audio and is_original(selected_audio)),
            })
    return options


def choose_video(options: list[dict], request: DownloadRequest) -> dict:
    """Choose an actual source resolution, then prefer compatible streams."""
    code = request.audioLanguage.replace("_", "-").lower()
    if code == "original" and options:
        code = min(options, key=lambda o: (not o["audio_original"], (o["audio_language"] or "").endswith("-desc"), o["audio_language"] == "und", o["audio_language"] or ""))["audio_language"]
    candidates = [o for o in options if
                  o["audio_language"] == code and
                  (request.videoCodec == "auto" or o["codec"] == request.videoCodec) and
                  (request.videoContainer == "auto" or o["container"] == request.videoContainer) and
                  (not request.embedSubtitles or o["container"] in {"mp4", "mkv"})]
    if request.videoQuality not in {"auto", "max"}:
        candidates = [o for o in candidates if o["height"] == int(request.videoQuality)]
    if not candidates:
        raise AppError("format.unavailable", "This combination is not available. Choose Automatic or another format, codec, or resolution.")
    if request.videoQuality == "auto":
        below = [o for o in candidates if 0 < o["height"] <= 720]
        target = max(o["height"] for o in below) if below else min(o["height"] for o in candidates)
        candidates = [o for o in candidates if o["height"] == target]
    elif request.videoQuality == "max":
        highest = max(o["height"] for o in candidates)
        candidates = [o for o in candidates if o["height"] == highest]
    preference = min(VIDEO_PREFERENCES.index((o["codec"], o["container"])) for o in candidates)
    candidates = [o for o in candidates if VIDEO_PREFERENCES.index((o["codec"], o["container"])) == preference]
    return max(candidates, key=lambda o: (o["height"], o["fps"], o["tbr"], o["video_id"], o["audio_id"] or ""))


@dataclass
class DownloadPlan:
    """Options for downloading and converting media."""

    selector: str
    container: str | None = None
    postprocessors: list[dict] = field(default_factory=list)
    audio_conversion: tuple[str, str | None] | None = None
    audio_language: str | None = None


def audio_conversion(request: DownloadRequest) -> tuple[str, str] | None:
    """Custom FFmpeg conversion settings, independent of source selection."""
    if request.downloadMode == "audio" and request.audioFormat in {"m4a", "opus"}:
        return request.audioFormat, request.audioBitrate
    return None


def build_plan(request: DownloadRequest, formats: list[dict] | None = None) -> DownloadPlan:
    """Prefer requested streams; fail rather than silently change codec or quality."""
    code = resolve_language(formats, request.audioLanguage) if formats is not None else None
    if formats is None and request.audioLanguage != "original":
        raise AppError("format.unavailable", "Source formats are required to select an audio language.")
    if request.downloadMode == "audio":
        codec = request.audioFormat
        selector = "ba[ext=m4a]/ba/b" if codec == "m4a" else "ba/b"
        if formats is not None:
            if code is None:
                raise AppError("format.unavailable", "This source does not contain an audio track.")
            selector = str(choose_audio(formats, code, codec)["format_id"])
        plan = DownloadPlan(selector, audio_language=code)
        if codec == "best":
            plan.postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "best"})
        if codec in {"mp3", "ogg", "wav", "flac", "m4a", "opus"}:
            if codec in {"m4a", "opus"}:
                plan.audio_conversion = audio_conversion(request)
            else:
                pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
                if codec in {"mp3", "ogg"}:
                    pp["preferredquality"] = request.audioBitrate
                plan.postprocessors.append(pp)
        return plan
    if request.videoCodec == "h264" and request.videoContainer == "webm":
        raise AppError("format.unavailable", "WebM does not support H.264. Choose MP4 or MKV, or use VP9 or AV1.")
    if request.embedSubtitles and request.videoContainer == "webm":
        raise AppError("format.unavailable", "To include subtitles in this app, choose MP4, MKV, or Automatic.")
    if formats is not None:
        choice = choose_video(video_options(formats, code), request)
        selector = choice["video_id"] + ("+" + choice["audio_id"] if choice["audio_id"] else "")
        return DownloadPlan(selector, choice["container"], audio_language=code)
    requested_codec = ("vp9" if request.videoContainer == "webm" else "h264") if request.videoCodec == "auto" else request.videoCodec
    codec = {"h264": "avc1", "vp9": "vp9", "av1": "av01"}[requested_codec]
    quality = "720" if request.videoQuality == "auto" else request.videoQuality
    height = "" if quality == "max" else f"[height<={quality}]"
    selector = f"bv{height}[vcodec^={codec}]+ba/b{height}[vcodec^={codec}]"
    container = request.videoContainer if request.videoContainer != "auto" else {"h264": "mp4", "vp9": "webm", "av1": "webm"}[requested_codec]
    if container == "webm":
        selector = f"bv{height}[vcodec^={codec}]+ba[acodec^=opus]/b{height}[vcodec^={codec}]"
    elif container == "mp4":
        selector = f"bv{height}[vcodec^={codec}]+ba[acodec^=mp4a]/b{height}[vcodec^={codec}]"
    return DownloadPlan(selector, container)


def describe_plan(plan: DownloadPlan) -> str:
    """Describe the selected format in English."""
    return f"{plan.selector} → {plan.container or 'native audio'}"

"""yt-dlp format selection without video re-encoding."""
from dataclasses import dataclass, field

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


def video_options(formats: list[dict]) -> list[dict]:
    """Return only combinations that can be downloaded without video recoding."""
    audio = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none") and f.get("format_id")]
    source_has_audio = any(f.get("acodec") not in (None, "none") for f in formats)
    options = []
    for stream in formats:
        codec = video_codec(stream.get("vcodec"))
        if not codec or not stream.get("format_id"):
            continue
        has_audio = stream.get("acodec") not in (None, "none")
        for container in ("mp4", "webm", "mkv"):
            if container == "webm" and codec == "h264":
                continue
            if has_audio:
                if not compatible_audio(stream["acodec"], container):
                    continue
                audio_id = None
            else:
                candidates = [f for f in audio if compatible_audio(f.get("acodec"), container)]
                if not candidates:
                    if source_has_audio:
                        continue
                    audio_id = None  # A genuinely silent source still has a valid video.
                else:
                    best = max(candidates, key=lambda f: (f.get("abr") or f.get("tbr") or 0, f.get("asr") or 0))
                    audio_id = str(best["format_id"])
            options.append({
                "codec": codec, "container": container, "height": stream.get("height") or 0,
                "video_id": str(stream["format_id"]), "audio_id": audio_id,
                "fps": stream.get("fps") or 0, "tbr": stream.get("tbr") or 0,
            })
    return options


def choose_video(options: list[dict], request: DownloadRequest) -> dict:
    """Choose an actual source resolution, then prefer compatible streams."""
    candidates = [o for o in options if
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
    return max(candidates, key=lambda o: (o["height"], o["fps"], o["tbr"]))


@dataclass
class DownloadPlan:
    """Options for downloading and converting media."""

    selector: str
    container: str | None = None
    postprocessors: list[dict] = field(default_factory=list)
    audio_conversion: tuple[str, str | None] | None = None


def build_plan(request: DownloadRequest, formats: list[dict] | None = None) -> DownloadPlan:
    """Prefer requested streams; fail rather than silently change codec or quality."""
    if request.downloadMode == "audio":
        codec = request.audioFormat
        selector = "ba[ext=m4a]/ba/b" if codec == "m4a" else "ba/b"
        plan = DownloadPlan(selector)
        if codec == "best":
            plan.postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "best"})
        if codec in {"mp3", "ogg", "wav", "flac", "m4a", "opus"}:
            if codec in {"m4a", "opus"}:
                plan.audio_conversion = (codec, request.audioBitrate)
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
        choice = choose_video(video_options(formats), request)
        selector = choice["video_id"] + ("+" + choice["audio_id"] if choice["audio_id"] else "")
        return DownloadPlan(selector, choice["container"])
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

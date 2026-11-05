"""yt-dlp format selection without video re-encoding."""
from dataclasses import dataclass, field

from app.schemas import DownloadRequest


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
        if codec in {"mp3", "ogg", "wav", "flac", "m4a", "opus"}:
            if codec in {"m4a", "opus"}:
                plan.audio_conversion = (codec, request.audioBitrate)
            else:
                pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
                if codec in {"mp3", "ogg"}:
                    pp["preferredquality"] = request.audioBitrate
                plan.postprocessors.append(pp)
        return plan
    codec = {"h264": "avc1", "vp9": "vp9", "av1": "av01"}[request.videoCodec]
    height = "" if request.videoQuality == "max" else f"[height<={request.videoQuality}]"
    selector = f"bv{height}[vcodec^={codec}]+ba/b{height}[vcodec^={codec}]"
    container = request.videoContainer if request.videoContainer != "auto" else {"h264": "mp4", "vp9": "webm", "av1": "webm"}[request.videoCodec]
    if container == "webm":
        selector = f"bv{height}[vcodec^={codec}]+ba[acodec^=opus]/b{height}[vcodec^={codec}]"
    elif container == "mp4":
        selector = f"bv{height}[vcodec^={codec}]+ba[acodec^=mp4a]/b{height}[vcodec^={codec}]"
    return DownloadPlan(selector, container)


def describe_plan(plan: DownloadPlan) -> str:
    """Describe the selected format in English."""
    return f"{plan.selector} → {plan.container or 'native audio'}"

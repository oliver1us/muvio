"""Public JSON request models."""
from typing import Literal

from pydantic import BaseModel, Field


class DownloadRequest(BaseModel):
    """Cobalt-style download options with Muvio extensions."""

    url: str
    downloadMode: Literal["auto", "audio", "video"] = "auto"
    videoQuality: str = Field(default="auto", pattern=r"^(auto|max|[1-9][0-9]{0,4})$")
    videoContainer: Literal["auto", "mp4", "webm", "mkv"] = "auto"
    videoCodec: Literal["auto", "h264", "vp9", "av1"] = "auto"
    audioFormat: Literal["best", "mp3", "m4a", "opus", "ogg", "wav", "flac"] = "mp3"
    audioBitrate: Literal["320", "256", "128", "96", "64"] = "320"
    audioLanguage: str = Field(default="original", min_length=1, max_length=64)
    subtitleLang: str | None = None
    subtitleAutomatic: bool = False
    downloadSubtitles: bool = False
    embedSubtitles: bool = False
    subtitleFormat: Literal["srt", "vtt"] = "srt"
    embedThumbnail: bool = True
    embedMetadata: bool = True
    filenameStyle: Literal["basic", "pretty", "classic"] = "pretty"
    transcribe: bool = False
    transcriptionMode: Literal["quick", "studio"] = "quick"
    enableAiCleanup: bool = False
    playlist: bool = False


class TranscribeRequest(BaseModel):
    """Options for a URL or uploaded audio transcription."""

    url: str | None = None
    mode: Literal["quick", "studio"] = "quick"
    language: str | None = None
    vocalFocus: bool = False
    separateVocals: bool = False
    aiCleanup: bool = False
    title: str = ""
    artist: str = ""


class SubtitleRequest(BaseModel):
    """A single source subtitle download."""

    url: str
    lang: str
    automatic: bool = False
    format: Literal["srt", "vtt"] = "srt"

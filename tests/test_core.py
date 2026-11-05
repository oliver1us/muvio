"""Offline regression tests for the API and pure media helpers."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.engine.formats import build_plan
from app.engine.naming import sanitize, unique
from app.engine.subtitles import vtt_to_srt
from app.errors import CODES
from app.schemas import DownloadRequest
from app.transcription.studio import export, merge
from main import app


def test_format_plans() -> None:
    """Audio bitrate and MKV stream-copy plans are consistent."""
    audio = build_plan(DownloadRequest(url="https://example.com", downloadMode="audio"))
    assert audio.selector == "ba/b"
    assert audio.postprocessors[0] == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
    video = build_plan(DownloadRequest(url="https://example.com", videoContainer="mkv"))
    assert video.container == "mkv" and not video.postprocessors


def test_subtitles() -> None:
    """WebVTT headers and notes do not leak into SubRip cues."""
    source = "WEBVTT\n\nNOTE old note\nignored\n\n00:01.000 --> 00:02.500\nHello\n"
    assert vtt_to_srt(source) == "1\n00:00:01,000 --> 00:00:02,500\nHello\n"


def test_names(tmp_path: Path) -> None:
    """Windows-unsafe characters are removed without losing Unicode."""
    assert sanitize("My: Song? <bad> | name") == "My Song bad name"
    assert sanitize("á ñ 日本語") == "á ñ 日本語"
    first = tmp_path / "Song.mp3"
    first.touch()
    assert unique(first).name == "Song (1).mp3"


def test_merge_and_export(tmp_path: Path) -> None:
    """Higher-confidence vocals replace aligned speech and export all formats."""
    original = [{"start": 0, "end": 2, "text": "wrong", "confidence": 0.4, "words": []}]
    vocals = [{"start": 0.1, "end": 2, "text": "right", "confidence": 0.9, "words": []}]
    rows = merge(original, vocals)
    assert len(rows) == 1 and rows[0]["text"] == "right"
    paths = export({"language": "en", "duration": 2, "segments": rows}, tmp_path, "Song")
    assert set(paths) == {"txt", "srt", "vtt", "json"}
    assert "00:00:00,100 --> 00:00:02,000" in paths["srt"].read_text()


def test_error_taxonomy_and_health() -> None:
    """All documented failures exist and the app starts without a model."""
    assert {"url.invalid", "url.unsupported", "media.private", "ffmpeg.failed", "whisper.model_missing", "demucs.failed", "llm.failed", "file.not_found", "job.cancelled", "rate.limited"} <= CODES
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/files/../../etc/passwd").status_code in (404, 403)
        assert client.get("/api/settings").status_code == 200


def test_literal_values() -> None:
    """Invalid codec selections are rejected during request parsing."""
    from pydantic import ValidationError
    try:
        DownloadRequest(url="https://example.com", videoCodec="hevc")
    except ValidationError:
        pass
    else:
        raise AssertionError("Invalid codec accepted")

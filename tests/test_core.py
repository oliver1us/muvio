"""Offline regression tests for the API and pure media helpers."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.engine.formats import build_plan
from app.engine import ffmpeg, ytdlp
from app.engine.naming import filename, sanitize, unique
from app.engine.subtitles import vtt_to_srt
from app.errors import CODES
from app.schemas import DownloadRequest
from app.transcription.studio import export, merge
from app.transcription import engine
from app.errors import AppError
from main import app


def test_format_plans() -> None:
    """Audio bitrate and MKV stream-copy plans are consistent."""
    audio = build_plan(DownloadRequest(url="https://example.com", downloadMode="audio"))
    assert audio.selector == "ba/b"
    assert audio.postprocessors[0] == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
    video = build_plan(DownloadRequest(url="https://example.com", videoContainer="mkv"))
    assert video.container == "mkv" and not video.postprocessors


def test_mp3_cover_postprocessor(monkeypatch, tmp_path: Path) -> None:
    """Default MP3 downloads use yt-dlp's valid thumbnail postprocessor."""
    options_seen = []

    class FakeYoutubeDL:
        def __init__(self, options):
            options_seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def download(self, urls):
            (tmp_path / "song.mp3").touch()

    monkeypatch.setattr(ytdlp, "validate_url", lambda url: None)
    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    for embed in (True, False):
        ytdlp.download(
            "https://example.com/song", tmp_path,
            DownloadRequest(url="https://example.com/song", downloadMode="audio", embedThumbnail=embed),
            lambda *_: None, lambda: False,
        )

    enabled, disabled = options_seen
    assert enabled["writethumbnail"] is True
    assert [pp["key"] for pp in enabled["postprocessors"]] == ["FFmpegExtractAudio", "EmbedThumbnail", "FFmpegMetadata"]
    assert disabled["writethumbnail"] is False
    assert [pp["key"] for pp in disabled["postprocessors"]] == ["FFmpegExtractAudio", "FFmpegMetadata"]


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
    assert filename("classic", {"title": "Song", "uploader": "Artist", "id": "abc"}, "mp3") == "Artist - Song [abc].mp3"
    assert filename("basic", {"title": "á" * 300}, "mp3").endswith(".mp3")
    assert len(filename("basic", {"title": "á" * 300}, "mp3")) <= 180


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
        missing = client.get("/api/jobs/not-a-job")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "file.not_found"
        assert client.get("/api/files/../../etc/passwd").status_code in (404, 403)
        assert client.get("/api/settings").status_code == 200


def test_header_activity_and_favicon() -> None:
    """The activity popover and favicon are served with the static UI."""
    with TestClient(app) as client:
        page = client.get("/").text
        assert 'id="activityButton"' in page
        assert 'id="activityPanel"' in page
        assert 'class="history"' not in page
        assert 'rel="icon" type="image/svg+xml" href="/favicon.svg"' in page
        icon = client.get("/favicon.svg")
        assert icon.status_code == 200
        assert '<svg ' in icon.text


def test_literal_values() -> None:
    """Invalid codec selections are rejected during request parsing."""
    from pydantic import ValidationError
    try:
        DownloadRequest(url="https://example.com", videoCodec="hevc")
    except ValidationError:
        pass
    else:
        raise AssertionError("Invalid codec accepted")


def test_ffmpeg_audio_conversion(tmp_path: Path) -> None:
    """The configured bitrate and container produce valid audio."""
    if not ffmpeg.available():
        return
    async def verify() -> None:
        source = tmp_path / "source.wav"
        await ffmpeg.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(source))
        output = await ffmpeg.extract_audio(source, tmp_path / "result.m4a", "m4a", "128")
        info = await ffmpeg.probe(output)
        assert info["streams"][0]["codec_name"] == "aac"
    asyncio.run(verify())


def test_missing_whisper_model_is_explicit(monkeypatch, tmp_path: Path) -> None:
    """Missing local models cannot silently initiate a network download."""
    from app.config import settings
    monkeypatch.setattr(settings, "models_dir", tmp_path)
    monkeypatch.setattr(settings, "whisper_model", "large-v3-turbo")
    monkeypatch.setattr(engine, "_model", None)
    try:
        engine.transcribe(tmp_path / "audio.wav")
    except AppError as exc:
        assert exc.code == "whisper.model_missing"
    else:
        raise AssertionError("Missing Whisper model accepted")

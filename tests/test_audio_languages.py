"""Offline multilingual regressions: region/order must not replace originals."""
import asyncio
from itertools import permutations

import pytest
from fastapi.testclient import TestClient

from app.engine import ytdlp
from app.engine.audio import audio_tracks, resolve_language
from app.engine.formats import build_plan, choose_video, video_options
from app.errors import AppError
from app.schemas import DownloadRequest
from main import app


def audio(code, *, original=False, bitrate=128, codec="mp4a.40.2", name=None):
    return {"format_id": name or f"audio-{code}", "vcodec": "none", "acodec": codec,
            "abr": bitrate, "language": code, "language_preference": 10 if original else 5}


def video(**values):
    return {"format_id": "video", "vcodec": "avc1.64001f", "acodec": "none", "height": 720, **values}


def request(**values):
    return DownloadRequest(url="https://example.com/video", **values)


@pytest.mark.parametrize("mode", ["video", "audio"])
@pytest.mark.parametrize("dub_bitrate", [128, 320])
def test_original_wins_over_regional_default_order_and_bitrate(mode, dub_bitrate):
    tracks = [audio("hi", bitrate=dub_bitrate), audio("en", original=True), audio("es", bitrate=256)]
    for ordered in permutations(tracks):
        plan = build_plan(request(downloadMode=mode), [video(), *ordered])
        assert plan.audio_language == "en"
        assert plan.selector == ("video+audio-en" if mode == "video" else "audio-en")


@pytest.mark.parametrize("mode", ["video", "audio"])
def test_explicit_spanish_and_regional_variants_never_fall_back(mode):
    formats = [video(), audio("en", original=True), audio("es-419"), audio("es-ES")]
    plan = build_plan(request(downloadMode=mode, audioLanguage="es-419"), formats)
    assert plan.audio_language == "es-419" and plan.selector.endswith("audio-es-419")
    with pytest.raises(AppError, match="no longer available"):
        build_plan(request(downloadMode=mode, audioLanguage="es"), formats)
    with pytest.raises(AppError, match="no longer available"):
        build_plan(request(downloadMode=mode, audioLanguage="es-419"), [video(), audio("en", original=True)])


def test_language_precedes_container_compatibility_and_resolution():
    formats = [video(), audio("hi"), audio("en", original=True, codec="opus")]
    plan = build_plan(request(), formats)
    assert (plan.container, plan.audio_language, plan.selector) == ("mkv", "en", "video+audio-en")
    assert choose_video(video_options(formats), request())["audio_language"] == "en"
    with pytest.raises(AppError, match="not available"):
        build_plan(request(videoContainer="mp4"), formats)
    # A combined Hindi stream cannot stand in for the original English track.
    formats = [video(format_id="hi-combined", height=720, acodec="mp4a.40.2", language="hi"),
               video(format_id="en-combined", height=360, acodec="mp4a.40.2", language="en", language_preference=10)]
    assert build_plan(request(), formats).selector == "en-combined"


def test_unknown_and_unmarked_languages_are_honest_and_deterministic():
    unknown = {"format_id": "unknown", "acodec": "opus", "vcodec": "none"}
    assert audio_tracks([unknown]) == [{"language": "und", "is_original": False}]
    assert resolve_language([unknown]) == "und"
    assert resolve_language([video()]) is None
    for tracks in permutations([audio("hi"), audio("en"), unknown, audio("en-desc")]):
        assert resolve_language(list(tracks)) == "en"
        assert not any(track["is_original"] for track in audio_tracks(list(tracks)))
    with pytest.raises(AppError, match="no longer available"):
        build_plan(request(audioLanguage="en"), [video()])


def test_notes_originals_deduplication_and_stable_quality_ties():
    original = audio("es", name="original-z") | {"language_preference": None, "note": "Spanish (original)"}
    another = original | {"format_id": "original-a"}
    for tracks in permutations([original, another, audio("es", bitrate=320)]):
        assert audio_tracks(list(tracks)) == [{"language": "es", "is_original": True}]
        assert build_plan(request(downloadMode="audio"), list(tracks)).selector == "original-z"
        assert build_plan(request(), [video(), *tracks]).selector == "video+original-z"


def test_m4a_preference_stays_within_selected_language():
    formats = [audio("en", original=True, codec="opus"), audio("hi", codec="mp4a.40.2")]
    plan = build_plan(request(downloadMode="audio", audioFormat="m4a"), formats)
    assert plan.selector == "audio-en" and plan.audio_conversion == ("m4a", "320")


def test_info_preserves_language_metadata_and_language_specific_options(monkeypatch):
    formats = [video(), audio("hi", bitrate=320), audio("en", original=True), audio("es-419")]

    class FakeYoutubeDL:
        def __init__(self, _):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, *_args, **_kwargs):
            return {"id": "test", "title": "Multilingual", "formats": formats}

    monkeypatch.setattr(ytdlp, "validate_url", lambda _: None)
    monkeypatch.setattr(ytdlp, "_cache", {})
    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    data = ytdlp.info("https://example.com/video")
    assert data["defaultAudioLanguage"] == "en"
    assert data["audioTracks"][0] == {"language": "en", "is_original": True}
    assert data["audioStreams"][1]["language_preference"] == 10
    assert {option["audio_language"] for option in data["videoOptions"]} == {"en", "hi", "es-419"}
    assert choose_video(data["videoOptions"], request())["audio_id"] == "audio-en"


@pytest.mark.parametrize("mode", ["audio", "video"])
def test_api_validates_and_pins_language_before_enqueuing(monkeypatch, mode):
    from app.api import routes
    from app.queue.jobs import Job
    from main import _requests

    _requests.clear()
    formats = [video(), audio("hi"), audio("en", original=True)]
    data = {"entries": [], "is_live": False, "formats": formats, "videoStreams": formats[:1],
            "audioStreams": formats[1:], "subtitles": []}
    monkeypatch.setattr(ytdlp, "info", lambda _: data)
    submitted = []

    def submit(kind, url, operation, **_):
        submitted.append(operation)
        return Job("language-test", kind, url)

    monkeypatch.setattr(routes.queue, "submit", submit)
    received = []

    async def operation(_job, payload):
        received.append(payload)

    monkeypatch.setattr(routes, "download_operation", operation)
    with TestClient(app) as client:
        response = client.post("/api/download", json={"url": "https://example.com/video", "downloadMode": mode, "audioLanguage": "es"})
        assert response.status_code == 400 and not submitted
        assert response.json()["error"]["code"] == "format.unavailable"
        response = client.post("/api/download", json={"url": "https://example.com/video", "downloadMode": mode})
        assert response.status_code == 200
    asyncio.run(submitted[0](Job("language-test", "download")))
    assert received[0].audioLanguage == "en"


@pytest.mark.parametrize("mode", ["audio", "video"])
def test_download_cannot_replace_a_disappeared_language(monkeypatch, tmp_path, mode):
    monkeypatch.setattr(ytdlp, "validate_url", lambda _: None)
    monkeypatch.setattr(ytdlp, "info", lambda _: {"formats": [video(), audio("hi")]})
    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", lambda _: pytest.fail("Must fail before starting a download"))
    with pytest.raises(AppError, match="no longer available"):
        ytdlp.download("https://example.com/video", tmp_path, request(downloadMode=mode, audioLanguage="en"), lambda *_: None, lambda: False)

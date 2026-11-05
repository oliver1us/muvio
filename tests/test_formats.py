"""Offline tests for automatic selection and source-aware format validation."""
import pytest
from fastapi.testclient import TestClient

from app.engine import ytdlp
from app.engine.formats import build_plan, choose_video, video_options
from app.engine.subtitles import srt_to_vtt
from app.errors import AppError
from app.schemas import DownloadRequest
from main import app


def video(height, codec="avc1.64001f", audio="none", name=None):
    return {"format_id": name or f"{codec}-{height}", "height": height, "vcodec": codec, "acodec": audio, "fps": 30, "tbr": height * 2}


def audio(codec="mp4a.40.2", name="aac"):
    return {"format_id": name, "vcodec": "none", "acodec": codec, "abr": 128}


def request(**values):
    return DownloadRequest(url="https://example.com/video", **values)


@pytest.mark.parametrize(("heights", "expected"), [
    ([2160, 1440, 1080, 720, 480], 720),
    ([1080, 480, 360], 480),
    ([480, 360, 240], 480),
    ([240, 144], 240),
    ([1440, 1080], 1080),
])
def test_automatic_resolution(heights, expected):
    choice = choose_video(video_options([*(video(h) for h in heights), audio()]), request())
    assert choice["height"] == expected
    assert choice["container"] == "mp4" and choice["codec"] == "h264"


def test_maximum_and_explicit_quality_use_actual_source_combinations():
    formats = [video(480), video(1080, "av01.0.08M.08"), audio(), audio("opus", "opus")]
    assert choose_video(video_options(formats), request())["height"] == 480
    maximum = choose_video(video_options(formats), request(videoQuality="max"))
    assert maximum["height"] == 1080 and maximum["codec"] == "av1"
    exact = choose_video(video_options(formats), request(videoQuality="1080"))
    assert exact["height"] == 1080 and exact["codec"] == "av1"
    with pytest.raises(AppError, match="not available"):
        build_plan(request(videoCodec="h264", videoQuality="1080"), formats)


def test_automatic_cap_applies_across_source_codecs():
    formats = [video(1080), video(480), video(720, "vp9"), audio(), audio("opus", "opus")]
    choice = choose_video(video_options(formats), request())
    assert (choice["height"], choice["codec"], choice["container"]) == (720, "vp9", "webm")
    formats = [video(1440), video(1080, "vp9"), audio(), audio("opus", "opus")]
    assert choose_video(video_options(formats), request())["height"] == 1080


def test_missing_mp4_audio_uses_real_fallback_instead_of_failing_later():
    formats = [video(720), video(720, "vp9"), audio("opus", "opus")]
    choices = video_options(formats)
    assert not any(o["container"] == "mp4" for o in choices)
    choice = choose_video(choices, request())
    assert choice["container"] == "webm" and choice["codec"] == "vp9"
    plan = build_plan(request(), formats)
    assert plan.selector == "vp9-720+opus" and plan.container == "webm"


def test_invalid_containers_and_subtitle_embedding_are_blocked():
    with pytest.raises(AppError, match="WebM does not support H.264"):
        build_plan(request(videoCodec="h264", videoContainer="webm"))
    with pytest.raises(AppError, match="include subtitles"):
        build_plan(request(videoContainer="webm", embedSubtitles=True))
    formats = [video(720, "vp9"), audio("opus", "opus")]
    choice = choose_video(video_options(formats), request(embedSubtitles=True))
    assert choice["container"] == "mkv"


def test_silent_video_and_combined_streams():
    silent = build_plan(request(), [video(240)])
    assert silent.selector == "avc1.64001f-240"
    combined = build_plan(request(videoContainer="mkv"), [video(360, audio="mp4a.40.2", name="combined")])
    assert combined.selector == "combined" and combined.container == "mkv"
    # Do not turn a source with sound into silent video just because its audio
    # is available only in a combined stream.
    formats = [video(720), video(360, audio="mp4a.40.2", name="combined")]
    assert choose_video(video_options(formats), request())["height"] == 360


def test_native_audio_extracts_sound_from_combined_sources():
    plan = build_plan(request(downloadMode="audio", audioFormat="best"))
    assert plan.postprocessors == [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]


def test_low_and_nonstandard_resolutions_are_accepted():
    for height in (144, 240, 540, 4320):
        payload = request(videoQuality=str(height))
        assert build_plan(payload, [video(height), audio()]).container == "mp4"


def test_selected_single_stream_is_remuxed_to_the_promised_container(monkeypatch, tmp_path):
    seen = []

    class FakeYoutubeDL:
        def __init__(self, options):
            seen.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def download(self, urls):
            (tmp_path / "video.mkv").touch()

    monkeypatch.setattr(ytdlp, "validate_url", lambda _: None)
    monkeypatch.setattr(ytdlp, "info", lambda _: {"formats": [video(360, audio="mp4a.40.2", name="combined")]})
    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    ytdlp.download("https://example.com/video", tmp_path, request(videoContainer="mkv"), lambda *_: None, lambda: False)
    assert seen[0]["format"] == "combined"
    assert {"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"} in seen[0]["postprocessors"]


def test_srt_to_vtt_conversion():
    converted = srt_to_vtt("\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\nHello\r\n")
    assert converted == "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\nHello\n"


def test_vtt_download_converts_an_srt_only_source(monkeypatch, tmp_path):
    class FakeYoutubeDL:
        def __init__(self, options):
            assert options["subtitlesformat"].startswith("vtt/")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def download(self, urls):
            (tmp_path / "source.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,500\nHello\n")

    monkeypatch.setattr(ytdlp, "validate_url", lambda _: None)
    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    result = ytdlp.download_subtitle("https://example.com/video", tmp_path, "en", False, "vtt")
    assert result.suffix == ".vtt" and result.read_text().startswith("WEBVTT\n")


def test_api_rejects_impossible_combinations_before_enqueuing(monkeypatch):
    from app.api import routes
    from main import _requests

    _requests.clear()
    formats = [video(720), audio()]
    data = {"entries": [], "is_live": False, "formats": formats, "videoStreams": [formats[0]], "audioStreams": [formats[1]], "subtitles": []}
    monkeypatch.setattr(ytdlp, "info", lambda _: data)
    monkeypatch.setattr(routes.queue, "submit", lambda *_: pytest.fail("An invalid request must never be queued"))
    with TestClient(app) as client:
        for values in ({"videoCodec": "h264", "videoContainer": "webm"}, {"videoCodec": "av1"}, {"videoQuality": "1080"},
                       {"embedSubtitles": True, "subtitleLang": "en"}):
            response = client.post("/api/download", json={"url": "https://example.com/video", **values})
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "format.unavailable"
        response = client.post("/api/subtitle-download", json={"url": "https://example.com/video", "lang": "en"})
        assert response.status_code == 400


def test_api_automatic_audio_only_and_missing_audio(monkeypatch, tmp_path):
    import asyncio
    from app.api import routes
    from app.queue.jobs import Job
    from main import _requests

    _requests.clear()
    submitted = []

    def submit(kind, url, operation):
        submitted.append(operation)
        return Job("offline-audio", kind, url)

    data = {"entries": [], "is_live": False, "formats": [audio()], "videoStreams": [], "audioStreams": [audio()], "subtitles": []}
    monkeypatch.setattr(ytdlp, "info", lambda _: data)
    monkeypatch.setattr(routes.queue, "submit", submit)
    payloads = []

    def download(url, folder, payload, *_):
        payloads.append(payload)
        return tmp_path / "audio.mp3"

    monkeypatch.setattr(ytdlp, "download", download)
    with TestClient(app) as client:
        response = client.post("/api/download", json={"url": "https://example.com/audio"})
        assert response.status_code == 200
        assert response.json()["output"]["job_id"] == "offline-audio"
        data["audioStreams"] = []
        response = client.post("/api/download", json={"url": "https://example.com/silent", "downloadMode": "audio"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "format.unavailable"
    assert len(submitted) == 1
    asyncio.run(submitted[0](Job("offline-audio", "download")))
    assert payloads[0].downloadMode == "audio"
    assert (payloads[0].audioFormat, payloads[0].audioBitrate) == ("mp3", "320")


def test_frontend_static_assets_and_local_font():
    with TestClient(app) as client:
        page = client.get("/").text
        assert 'lang="en"' in page
        assert 'id="subtitlesPanel"' in page
        assert 'id="activityToast"' in page
        assert 'role="tablist"' in page
        assert "fonts.googleapis.com" not in page
        font = client.get("/fonts/jost-latin.woff2")
        assert font.status_code == 200 and font.content.startswith(b"wOF2")
        assert client.get("/js/media.mjs").status_code == 200

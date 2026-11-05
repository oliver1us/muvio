"""Real yt-dlp/FFmpeg checks using generated media and a loopback-only server."""
import json
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

from app.engine import ytdlp
from app.schemas import DownloadRequest

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe are required for real media integration tests",
)


def run(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def probe(path):
    return json.loads(run("ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)))


def video_hashes(path):
    return run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_data_hash", "sha256",
               "-show_entries", "packet=data_hash", "-of", "csv=p=0", str(path))


@pytest.fixture(scope="module")
def source_media(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-media")
    combined = root / "combined.mp4"
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=256x144:r=5:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(combined))
    run("ffmpeg", "-v", "error", "-i", str(combined), "-an", "-c:v", "copy", str(root / "silent.mp4"))
    run("ffmpeg", "-v", "error", "-i", str(combined), "-vn", "-c:a", "copy", str(root / "audio.m4a"))
    (root / "en.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:00.800\nA quiet afternoon.\n", encoding="utf-8")

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def offline_extractor(monkeypatch, source_media):
    root, origin = source_media
    state = {"separate": False, "silent": False}

    class OfflineMediaIE(InfoExtractor):
        _VALID_URL = r"https://offline\.example/(?P<id>clip)"

        def _real_extract(self, url):
            video = {"format_id": "video", "url": origin + "/silent.mp4", "ext": "mp4", "height": 144,
                     "width": 256, "vcodec": "avc1.64000c", "acodec": "none", "fps": 5}
            if state["separate"]:
                formats = [video, {"format_id": "audio", "url": origin + "/audio.m4a", "ext": "m4a",
                                   "vcodec": "none", "acodec": "mp4a.40.2", "abr": 64}]
            elif state["silent"]:
                formats = [video]
            else:
                formats = [video | {"format_id": "combined", "url": origin + "/combined.mp4", "acodec": "mp4a.40.2"}]
            return {"id": "clip", "title": "Offline clip", "uploader": "Muvio", "duration": 1,
                    "formats": formats, "subtitles": {"en": [{"url": origin + "/en.vtt", "ext": "vtt"}]}}

    class OfflineYoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self, options):
            super().__init__(options, auto_init=False)
            self.add_info_extractor(OfflineMediaIE())

    # Only this test extractor can run. Production URL validation is deliberately
    # bypassed for these generated loopback fixtures, never for a public request.
    monkeypatch.setattr(ytdlp, "validate_url", lambda _: None)
    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", OfflineYoutubeDL)
    monkeypatch.setattr(ytdlp, "_cache", {})
    return state, root


@pytest.mark.parametrize(("container", "separate", "silent"), [
    ("mkv", False, False), ("mp4", True, False), ("mp4", False, True),
])
def test_delivered_video_container_and_original_video_packets(offline_extractor, tmp_path, container, separate, silent):
    state, root = offline_extractor
    state.update(separate=separate, silent=silent)
    request = DownloadRequest(url="https://offline.example/clip", downloadMode="video", videoContainer=container)
    result = ytdlp.download(request.url, tmp_path / "result", request, lambda *_: None, lambda: False)
    details = probe(result)
    assert result.suffix == "." + container
    assert ("matroska" if container == "mkv" else "mp4") in details["format"]["format_name"]
    streams = details["streams"]
    assert any(stream["codec_name"] == "h264" and stream["height"] == 144 for stream in streams)
    assert any(stream["codec_type"] == "audio" for stream in streams) is not silent
    assert video_hashes(result) == video_hashes(root / "combined.mp4"), "Video packets must not be re-encoded"


@pytest.mark.parametrize("container", ["mp4", "mkv"])
def test_real_subtitle_embedding(offline_extractor, tmp_path, container):
    request = DownloadRequest(url="https://offline.example/clip", videoContainer=container,
                              embedSubtitles=True, subtitleLang="en")
    result = ytdlp.download(request.url, tmp_path / "subtitled", request, lambda *_: None, lambda: False)
    streams = probe(result)["streams"]
    assert any(stream["codec_type"] == "subtitle" for stream in streams)
    assert video_hashes(result) == video_hashes(offline_extractor[1] / "combined.mp4")


@pytest.mark.parametrize(("format", "codec"), [("best", "aac"), ("mp3", "mp3")])
def test_real_audio_extraction_from_combined_video(offline_extractor, tmp_path, format, codec):
    request = DownloadRequest(url="https://offline.example/clip", downloadMode="audio", audioFormat=format, embedThumbnail=False)
    result = ytdlp.download(request.url, tmp_path / "audio", request, lambda *_: None, lambda: False)
    streams = probe(result)["streams"]
    assert len(streams) == 1 and streams[0]["codec_name"] == codec
    assert streams[0]["codec_type"] == "audio"

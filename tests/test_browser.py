"""Optional Chromium interaction tests; all media/API requests stay offline."""
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect


@pytest.fixture(scope="module")
def static_url():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    root = Path(__file__).resolve().parents[1] / "app" / "web"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"Install Playwright Chromium to run browser tests: {exc}")
        yield instance
        instance.close()


def media(heights=(1080, 720, 480), codec="h264", container="mp4", sound=True, subtitles=None):
    return {
        "title": "A quiet afternoon", "uploader": "Muvio test", "duration": 185,
        "thumbnail": "/favicon.svg", "entries": [], "is_live": False,
        "audioStreams": [{"acodec": "mp4a.40.2"}] if sound else [],
        "videoOptions": [{"codec": codec, "container": container, "height": h} for h in heights],
        "subtitles": subtitles or [],
    }


@pytest.fixture
def ui(browser, static_url):
    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US", reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {"media": media(), "jobs": [], "requests": [], "settings": {
        "whisper_model": "large-v3-turbo", "demucs_enabled": False,
        "enable_llm_cleanup": False, "llm_provider": "ollama", "llm_base_url": "", "llm_model": "",
    }}

    def respond(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/events":
            route.fulfill(status=200, content_type="text/event-stream", body=": offline test\n\n")
            return
        payload = request.post_data_json if request.post_data and "application/json" in request.headers.get("content-type", "") else None
        state["requests"].append((request.method, path, payload))
        if path == "/api/session":
            result = {"identity": "anonymous", "administrator": state.get("admin", True), "accounts_enabled": False,
                      "persist_storage": True, "quota": state.get("quota", {"limit": 5, "remaining": 5, "next_available_at": None})}
        elif path == "/api/info":
            if state.get("info_error"):
                route.fulfill(status=400, json={"status": "error", "error": {"context": {"message": state["info_error"]}}})
                return
            result = state["media"]
        elif path == "/api/jobs":
            query = parse_qs(urlsplit(request.url).query)
            start, limit = int(query.get("cursor", [0])[0]), int(query.get("limit", [30])[0])
            result = {"items": state["jobs"][start:start + limit], "next_cursor": str(start + limit) if len(state["jobs"]) > start + limit else None}
        elif path.startswith("/api/jobs/"):
            job = next(job for job in state["jobs"] if job["id"] == path.rsplit("/", 1)[1])
            if request.method == "DELETE":
                job["status"] = "cancelled"
            result = job | {"url": "/api/files/ready?token=test", "downloads": job.get("downloads", {})}
        elif path.startswith("/api/files/"):
            route.fulfill(body="offline test file", headers={"Content-Disposition": 'attachment; filename="video.mp4"'})
            return
        elif path == "/api/settings":
            if request.method == "PUT":
                state["settings"].update(payload)
            result = state["settings"]
        else:
            kind = "subtitle" if path == "/api/subtitle-download" else "transcribe" if path == "/api/transcribe" else "download"
            job = {"id": str(len(state["jobs"]) + 1), "kind": kind, "status": "queued", "percent": 0, "stage": "Queued"}
            state["jobs"].append(job)
            result = {"status": "local-processing", "output": {"job_id": job["id"]}}
        route.fulfill(json=result)

    page.route("**/api/**", respond)
    page.goto(static_url)
    expect(page.locator("#fetch")).to_be_enabled()
    yield page, state
    context.close()
    assert errors == [], f"Uncaught browser errors: {errors}"


def find_media(page):
    page.locator("#url").fill("https://example.com/video")
    page.locator("#fetch").click()
    expect(page.locator("#panel")).to_be_visible()
    expect(page.locator("#fetch")).to_be_enabled()


def test_default_cards_tabs_and_local_font(ui):
    page, state = ui
    find_media(page)
    expect(page.locator("#videoSummary")).to_have_text("Automatic (MP4 · 720p)")
    expect(page.locator("#videoAdvanced")).not_to_have_attribute("open", "")
    expect(page.locator("#codec")).not_to_be_visible()
    assert page.evaluate("document.documentElement.lang") == "en"
    assert page.evaluate("async () => { await document.fonts.ready; return [...document.fonts].some(f => f.family === 'Jost' && f.status === 'loaded'); }")
    assert page.evaluate("performance.getEntriesByType('resource').filter(r => /fonts/.test(r.name)).every(r => new URL(r.name).origin === location.origin)")
    page.locator("#videoTab").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#audioTab")).to_be_focused()
    expect(page.locator("#audioPanel")).to_be_visible()
    expect(page.locator("#videoPanel")).not_to_be_visible()
    expect(page.locator("#audioSummary")).to_have_text("Automatic (MP3)")
    page.keyboard.press("End")
    expect(page.locator("#subtitlesTab")).to_be_focused()
    expect(page.locator("#noSubtitles")).to_be_visible()
    page.keyboard.press("Home")
    expect(page.locator("#videoTab")).to_be_focused()
    page.locator("#videoDownload").click()
    expect(page.locator("#toastText")).to_contain_text("Video download added")
    payload = next(body for method, path, body in state["requests"] if path == "/api/download")
    assert payload["videoQuality"] == payload["videoCodec"] == payload["videoContainer"] == "auto"
    assert payload["downloadMode"] == "video"


@pytest.mark.parametrize(("heights", "expected"), [((240, 144), 240), ((1440, 1080), 1080)])
def test_source_resolution_defaults(ui, heights, expected):
    page, state = ui
    state["media"] = media(heights)
    find_media(page)
    expect(page.locator("#videoSummary")).to_have_text(f"Automatic (MP4 · {expected}p)")
    if expected > 720:
        expect(page.locator("#videoDescription")).to_contain_text("Using the lowest source resolution")
    page.locator("#videoAdvanced > summary").click()
    values = page.locator("#quality option").evaluate_all("items => items.map(item => item.value)")
    assert values == ["auto", "max", *(str(h) for h in heights)]


def test_source_aware_advanced_options(ui):
    page, state = ui
    state["media"]["videoOptions"] = [
        {"codec": "h264", "container": "mp4", "height": 480},
        {"codec": "h264", "container": "mkv", "height": 480},
        {"codec": "vp9", "container": "webm", "height": 1080},
        {"codec": "vp9", "container": "mkv", "height": 1080},
    ]
    find_media(page)
    page.locator("#videoAdvanced > summary").click()
    expect(page.locator('#codec option[value="av1"]')).to_have_js_property("disabled", True)
    page.locator("#quality").select_option("max")
    expect(page.locator("#videoSummary")).to_have_text("WebM · 1080p")
    expect(page.locator("#videoWarning")).to_contain_text("compatible player")
    page.locator("#quality").select_option("1080")
    page.locator("#codec").select_option("h264")
    expect(page.locator('#container option[value="webm"]')).to_have_js_property("disabled", True)
    expect(page.locator('#quality option[value="1080"]')).to_have_js_property("disabled", True)
    expect(page.locator("#videoDownload")).to_be_disabled()
    expect(page.locator("#videoWarning")).to_contain_text("No download will start")
    page.locator("#resetVideo").click()
    expect(page.locator("#videoDownload")).to_be_enabled()
    expect(page.locator("#videoSummary")).to_have_text("Automatic (MP4 · 480p)")


def test_audio_defaults_and_quality_hints(ui):
    page, state = ui
    find_media(page)
    page.locator("#audioTab").click()
    page.locator("#audioDownload").click()
    payload = next(body for _, path, body in state["requests"] if path == "/api/download")
    assert (payload["audioFormat"], payload["audioBitrate"], payload["embedThumbnail"]) == ("mp3", "320", True)
    page.locator("#audioPanel .advanced > summary").click()
    page.locator("#bitrate").select_option("128")
    expect(page.locator("#audioSummary")).to_have_text("MP3 · 128 kbps")
    expect(page.locator("#audioBadge")).not_to_be_visible()
    page.locator("#audioFormat").select_option("flac")
    expect(page.locator("#bitrate")).to_be_disabled()
    expect(page.locator("#cover")).to_be_disabled()
    expect(page.locator("#audioHint")).to_contain_text("cannot recover")
    page.locator("#audioFormat").select_option("best")
    expect(page.locator("#audioSummary")).to_have_text("Original audio")
    expect(page.locator("#audioHint")).to_contain_text("source determines the file format")


def test_audio_only_and_silent_media(ui):
    page, state = ui
    state["media"] = media(heights=())
    find_media(page)
    expect(page.locator("#videoTab")).to_be_disabled()
    expect(page.locator("#audioPanel")).to_be_visible()
    state["media"] = media((240,), sound=False)
    find_media(page)
    expect(page.locator("#audioTab")).to_be_disabled()
    expect(page.locator("#videoPanel")).to_be_visible()
    expect(page.locator("#videoTagline")).to_contain_text("no sound")
    page.locator("#videoAdvanced > summary").click()
    expect(page.locator("#videoTechnical")).to_contain_text("No audio track")


def test_subtitle_dedup_search_download_and_embedding(ui):
    page, state = ui
    tracks = [{"lang": lang, "is_automatic": False, "ext": ext}
              for lang in ("en", "es", "fr", "de", "it", "ja", "ko", "pt", "nl") for ext in ("vtt", "srt")]
    tracks.append({"lang": "en", "is_automatic": True, "ext": "vtt"})
    state["media"] = media(subtitles=tracks)
    find_media(page)
    page.locator("#subtitlesTab").click()
    expect(page.locator("#subtitleTrack option")).to_have_count(10)
    expect(page.locator("#subtitleTrack")).to_have_value("en:false")
    page.locator("#subtitleSearch").fill("English")
    expect(page.locator("#subtitleTrack option")).to_have_count(2)
    page.locator("#subtitleTrack").select_option("en:true")
    page.locator("#subtitlesPanel .advanced > summary").click()
    page.locator("#subtitleFormat").select_option("vtt")
    page.locator("#subtitleDownload").click()
    expect(page.locator("#toastText")).to_contain_text("Subtitle download added")
    payload = next(body for _, path, body in state["requests"] if path == "/api/subtitle-download")
    assert payload == {"url": "https://example.com/video", "lang": "en", "automatic": True, "format": "vtt"}
    page.locator("#embedSelected").click()
    expect(page.locator("#videoPanel")).to_be_visible()
    expect(page.locator("#embedSubtitle")).to_have_value("en:true")
    expect(page.locator('#container option[value="webm"]')).to_have_js_property("disabled", True)
    page.locator("#videoDownload").click()
    expect(page.locator("#toastText")).to_contain_text("Video download added")
    payload = next(body for _, path, body in state["requests"] if path == "/api/download")
    assert payload["subtitleLang"] == "en" and payload["subtitleAutomatic"] and payload["embedSubtitles"]
    page.locator("#subtitlesTab").click()
    page.locator("#subtitleSearch").fill("no such language")
    expect(page.locator("#subtitleDownload")).to_be_disabled()
    expect(page.locator("#subtitleInfo")).to_contain_text("No matching language")


def test_activity_toast_expiry_and_file_saving(ui):
    page, state = ui
    find_media(page)
    page.clock.install()
    page.locator("#videoDownload").click()
    expect(page.locator("#activityToast")).to_be_visible()
    expect(page.locator("#activityCount")).to_have_text("1")
    page.clock.fast_forward(4600)
    expect(page.locator("#activityToast")).not_to_be_visible()
    page.locator("#activityButton").click()
    expect(page.locator("#activityPanel")).to_be_visible()
    expect(page.locator("#jobs")).to_contain_text("In queue")
    state["jobs"][0].update(status="done", filename="video.mp4", percent=100)
    page.locator("#refresh").click()
    link = page.get_by_role("link", name="Save file")
    expect(link).to_be_visible()
    with page.expect_download() as download:
        link.click()
    assert download.value.suggested_filename == "video.mp4"
    page.keyboard.press("Escape")
    expect(page.locator("#activityPanel")).not_to_be_visible()
    expect(page.locator("#activityButton")).to_be_focused()
    page.locator("#dismissToast").click()
    expect(page.locator("#activityToast")).not_to_be_visible()


def test_activity_progress_cancel_and_installed_models(ui):
    page, state = ui
    state["jobs"] = [
        {"id": "running", "kind": "download", "status": "running", "stage": "Downloading", "percent": 42, "speed": 2000000, "eta": 10},
        {"id": "model", "kind": "model", "status": "done", "filename": "Whisper model", "percent": 100},
    ]
    page.locator("#activityButton").click()
    expect(page.locator("#activityProgress")).to_have_text("42%")
    expect(page.get_by_role("progressbar")).to_have_attribute("value", "42")
    expect(page.locator("#jobs")).to_contain_text("Model installed")
    expect(page.locator("#jobs a")).to_have_count(0)
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#jobs")).to_contain_text("Cancelled")
    expect(page.locator("#activityCount")).not_to_be_visible()


def test_collection_picker_uses_the_selected_link(ui):
    page, state = ui
    state["media"] = {"entries": [{"id": "one", "title": "First clip", "url": "https://example.com/first"}]}
    page.locator("#url").fill("https://example.com/collection")
    page.locator("#fetch").click()
    expect(page.locator("#picker")).to_be_visible()
    state["media"] = media()
    page.get_by_role("button", name="First clip").click()
    expect(page.locator("#panel")).to_be_visible()
    page.locator("#videoDownload").click()
    expect(page.locator("#toastText")).to_contain_text("Video download added")
    payload = next(body for _, path, body in state["requests"] if path == "/api/download")
    assert payload["url"] == "https://example.com/first"


def test_transcription_link_options_and_file_upload(ui):
    page, state = ui
    find_media(page)
    page.locator("#subtitlesTab").click()
    page.locator("#transcriptionOptions > summary").click()
    expect(page.locator("#ai")).not_to_be_checked()
    page.locator("#mode").select_option("studio")
    page.locator("#language").select_option("en")
    page.locator("#focus").check()
    page.locator("#transcribe").click()
    expect(page.locator("#toastText")).to_contain_text("Transcription added")
    payload = next(body for _, path, body in state["requests"] if path == "/api/transcribe")
    assert payload["mode"] == "studio" and payload["language"] == "en" and payload["vocalFocus"]
    assert not payload["aiCleanup"]
    page.locator(".upload > summary").click()
    page.locator("#upload").set_input_files({"name": "test.wav", "mimeType": "audio/wav", "buffer": b"RIFF-offline-fixture"})
    page.locator("#uploadButton").click()
    expect(page.locator("#toastText")).to_contain_text("File transcription added")
    assert len([path for _, path, _ in state["requests"] if path == "/api/transcribe"]) == 2


def test_settings_focus_trap_and_save(ui):
    page, state = ui
    page.locator("#settingsButton").click()
    expect(page.get_by_role("dialog", name="Settings")).to_be_visible()
    expect(page.locator("#closeSettings")).to_be_focused()
    assert page.locator("#main").evaluate("node => node.inert")
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#saveSettings")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#closeSettings")).to_be_focused()
    page.locator("#authKey").fill("local-test-key")
    page.locator("#llmModel").fill("test-model")
    page.locator("#saveSettings").click()
    expect(page.locator("#drawer")).not_to_be_visible()
    expect(page.locator("#settingsButton")).to_be_focused()
    expect(page.locator("#toastText")).to_have_text("Settings saved.")
    assert page.evaluate("localStorage.getItem('muvio-api-key')") == "local-test-key"
    assert state["settings"]["llm_model"] == "test-model"
    page.locator("#settingsButton").click()
    page.keyboard.press("Escape")
    expect(page.locator("#settingsButton")).to_be_focused()


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_responsive_layout_and_sticky_activity(ui, width):
    page, state = ui
    page.set_viewport_size({"width": width, "height": 844})
    state["media"]["title"] = "An unusually long title " * 20
    find_media(page)
    page.locator("#videoAdvanced > summary").click()
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    header = page.locator(".header").bounding_box()
    assert header and header["y"] == 0
    expect(page.locator("#activityButton")).to_be_in_viewport()
    page.locator("#activityButton").click()
    panel = page.locator("#activityPanel").bounding_box()
    assert panel and panel["x"] >= 0 and panel["x"] + panel["width"] <= width
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.keyboard.press("Escape")
    page.locator("#settingsButton").click()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_error_and_unknown_fragment_are_safe(ui):
    page, state = ui
    state["info_error"] = "This source is unavailable. Try another public link."
    page.locator("#url").fill("https://example.com/video")
    page.locator("#fetch").click()
    expect(page.locator("#message")).to_have_text(state["info_error"])
    expect(page.locator("#message")).to_be_focused()
    del state["info_error"]
    page.evaluate('location.hash = \'unexpected"]\'')
    find_media(page)
    expect(page.locator("#videoPanel")).to_be_visible()
    expect(page.locator("#message")).not_to_be_visible()


def multilingual_media():
    return media() | {
        "audioTracks": [{"language": "en", "is_original": True}, {"language": "es", "is_original": False},
                        {"language": "hi", "is_original": False}],
        "defaultAudioLanguage": "en",
        "videoOptions": [{"codec": "h264", "container": "mp4", "height": 720,
                          "audio_language": code, "audio_original": code == "en"} for code in ("en", "es", "hi")],
    }


def test_audio_language_preview_selection_payload_and_reset(ui):
    page, state = ui
    state["media"] = multilingual_media()
    find_media(page)
    expect(page.locator("#videoAudioLanguage")).to_be_visible()
    expect(page.locator("#videoAudioLanguage")).to_have_value("original")
    expect(page.locator("#videoAudioSummary")).to_have_text("Audio: English · Original")
    page.locator("#videoDownload").click()
    expect(page.locator("#toastText")).to_contain_text("Video download added")
    payload = [body for _, path, body in state["requests"] if path == "/api/download"][-1]
    assert payload["audioLanguage"] == "en"
    page.locator("#videoAudioLanguage").select_option("es")
    expect(page.locator("#videoAudioSummary")).to_have_text("Audio: Spanish")
    page.locator("#videoDownload").click()
    expect(page.locator("#activityCount")).to_have_text("2")
    payload = [body for _, path, body in state["requests"] if path == "/api/download"][-1]
    assert payload["audioLanguage"] == "es" and payload["subtitleLang"] is None
    page.locator("#videoAdvanced > summary").click()
    page.locator("#resetVideo").click()
    expect(page.locator("#videoAudioLanguage")).to_have_value("original")
    expect(page.locator("#videoAudioSummary")).to_have_text("Audio: English · Original")
    page.locator("#audioTab").click()
    expect(page.locator("#audioTrackSummary")).to_have_text("Audio: English · Original")
    page.locator("#audioLanguage").select_option("hi")
    expect(page.locator("#audioTrackSummary")).to_have_text("Audio: Hindi")
    page.locator("#audioDownload").click()
    expect(page.locator("#activityCount")).to_have_text("3")
    payload = [body for _, path, body in state["requests"] if path == "/api/download"][-1]
    assert payload["audioLanguage"] == "hi" and payload["downloadMode"] == "audio"


def test_audio_language_changes_available_video_combinations(ui):
    page, state = ui
    data = multilingual_media()
    data["videoOptions"] = [option | {"container": "mkv" if option["audio_language"] == "es" else "mp4"}
                            for option in data["videoOptions"]]
    state["media"] = data
    find_media(page)
    page.locator("#videoAudioLanguage").select_option("es")
    expect(page.locator("#videoSummary")).to_have_text("MKV · 720p")
    page.locator("#videoAdvanced > summary").click()
    expect(page.locator('#container option[value="mp4"]')).to_have_js_property("disabled", True)
    page.locator("#container").select_option("mkv")
    page.locator("#videoAudioLanguage").select_option("en")
    expect(page.locator("#videoDownload")).to_be_disabled()
    expect(page.locator("#videoAudioSummary")).to_contain_text("not available together")
    page.locator("#resetVideo").click()
    expect(page.locator("#videoDownload")).to_be_enabled()


def test_missing_original_metadata_and_unknown_language_are_not_guessed(ui):
    page, state = ui
    state["media"] = media() | {
        "audioTracks": [{"language": "und", "is_original": False}], "defaultAudioLanguage": "und",
        "videoOptions": [{"codec": "h264", "container": "mp4", "height": 720,
                          "audio_language": "und", "audio_original": False}],
    }
    find_media(page)
    expect(page.locator("#videoAudioSummary")).to_contain_text("Unknown language")
    expect(page.locator("#videoAudioSummary")).to_contain_text("Original track not identified")
    expect(page.locator("#videoAudioLanguage option")).to_have_count(2)


def test_temporary_autosave_with_activity_closed_repeated_poll_and_reload(ui):
    page, state = ui
    page.evaluate("localStorage.setItem('muvio-api-key', 'offline-admin')")
    page.reload()
    page.clock.install()
    find_media(page)
    page.locator("#videoDownload").click()
    expect(page.locator("#activityPanel")).not_to_be_visible()
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    state["jobs"][0].update(status="done", storage_mode="temporary", availability="available",
                            filename="video.mp4", expires_at="2099-01-01T00:00:00+00:00")
    with page.expect_download() as download:
        page.clock.fast_forward(2600)
    assert download.value.suggested_filename == "video.mp4"
    page.clock.fast_forward(6000)
    expect(page.locator("#sessionStatus")).to_contain_text("Administrator")
    assert len(downloads) == 1
    page.locator("#activityButton").click()
    expect(page.get_by_role("link", name="Save file")).to_be_visible()
    expect(page.locator("#jobs")).to_contain_text("Temporary · available until")
    page.reload()
    page.clock.fast_forward(3000)
    expect(page.locator("#fetch")).to_be_enabled()
    assert len(downloads) == 1  # History and reload do not initiate more saves.


def test_anonymous_unlimited_session_hides_administration_and_expired_links(ui):
    page, state = ui
    state["admin"] = False
    state["quota"] = {"limit": 0, "remaining": None, "next_available_at": None}
    state["jobs"] = [{"id": "expired", "kind": "download", "status": "done", "storage_mode": "temporary",
                      "availability": "expired", "filename": "video.mp4"}]
    page.reload()
    expect(page.locator("#sessionStatus")).to_have_text("Anonymous session · Unlimited")
    expect(page.locator("#uploadOptions")).not_to_be_visible()
    page.locator("#activityButton").click()
    expect(page.locator("#jobs")).to_contain_text("Expired")
    expect(page.locator("#jobs a")).to_have_count(0)
    page.locator("#settingsButton").click()
    expect(page.locator("#connectKey")).to_be_visible()
    expect(page.locator("#adminSettings")).not_to_be_visible()


def test_initial_history_and_other_clients_do_not_autosave(ui):
    page, state = ui
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    state["jobs"] = [{"id": "other-tab", "kind": "subtitle", "status": "done", "storage_mode": "temporary",
                      "availability": "available", "filename": "captions.srt"}]
    page.reload()
    page.locator("#activityButton").click()
    expect(page.get_by_role("link", name="Save file")).to_be_visible()
    page.locator("#refresh").click()
    expect(page.locator("#jobs")).to_contain_text("captions.srt")
    assert downloads == []


def test_background_updates_keep_older_pages_and_refresh_their_status(ui):
    page, state = ui
    state["jobs"] = [{"id": str(index), "kind": "download", "status": "done", "availability": "available",
                      "storage_mode": "persistent", "filename": f"file-{index}.mp4"} for index in range(65)]
    page.evaluate("localStorage.setItem('muvio-api-key', 'offline-admin')")
    page.reload()
    page.clock.install()
    page.locator("#activityButton").click()
    expect(page.locator("#jobs article")).to_have_count(30)
    page.get_by_role("button", name="Show older activity").click()
    expect(page.locator("#jobs article")).to_have_count(60)
    state["jobs"][0]["availability"] = "missing"
    page.clock.fast_forward(3000)
    expect(page.locator("#jobs article")).to_have_count(60)
    expect(page.locator("#jobs article").first).to_contain_text("Not available")
    page.get_by_role("button", name="Show older activity").click()
    expect(page.locator("#jobs article")).to_have_count(65)
    expect(page.get_by_role("button", name="Show older activity")).not_to_be_visible()

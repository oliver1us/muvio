"""Offline acceptance checks for retention, authorization, quotas and recovery."""
import asyncio
import json
import sqlite3
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admission import admission
from app.blocking import run_sync
from app.config import Settings, settings
from app.engine import ytdlp
from app.errors import AppError
from app.identity import COOKIE, Principal
from app.queue.jobs import Job, Queue, queue
from app.queue.jobs import History
from app.storage import LeasedFileResponse, storage
from main import app

ADMIN = {"Authorization": "Api-Key offline-admin-key"}


@pytest.fixture
def offline(monkeypatch):
    stream = {"format_id": "combined", "vcodec": "avc1.64001f", "acodec": "mp4a.40.2", "ext": "mp4", "height": 720}
    data = {"title": "Offline", "duration": 10, "entries": [], "is_live": False, "formats": [stream],
            "videoStreams": [stream], "audioStreams": [stream], "subtitles": [{"lang": "en", "is_automatic": False}]}
    monkeypatch.setattr(ytdlp, "info", lambda _: data)

    def download(_url, folder, *_):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "original.part").write_bytes(b"partial")
        (folder / "cover.jpg").write_bytes(b"cover")
        result = folder / "result.mp4"
        result.write_bytes(b"0123456789")
        return result

    monkeypatch.setattr(ytdlp, "download", download)
    monkeypatch.setattr(ytdlp, "download_subtitle", lambda _url, folder, *_: write_result(folder, "result.srt"))
    return data


def write_result(folder, name="result.mp4"):
    path = folder / name
    path.write_bytes(b"0123456789")
    return path


def finish(client, job_id):
    client.portal.call(queue.pending.join)
    return client.get("/api/jobs/" + job_id).json()


def submit(client, **options):
    response = client.post("/api/download", json={"url": "https://offline.example/video"}, **options)
    assert response.status_code == 200, response.text
    return response.json()["output"]["job_id"]


@pytest.mark.parametrize("persistent", [True, False])
def test_media_roots_intermediates_and_private_history(monkeypatch, offline, persistent):
    monkeypatch.setattr(settings, "persist_storage", persistent)
    with TestClient(app) as client:
        job_id = submit(client)
        detail = finish(client, job_id)
        assert detail["status"] == "done"
        assert detail["availability"] == "available"
        assert bool(detail["expires_at"]) is not persistent
        expected = settings.output_dir / job_id if persistent else settings.temp_storage_dir / "jobs" / job_id
        assert (expected / "result.mp4").is_file()
        assert not (expected / "original.part").exists()
        assert not (expected / "cover.jpg").exists()
        assert client.get(detail["url"]).content == b"0123456789"
        assert not queue.jobs  # No full history in memory.
        if not persistent:
            assert not (settings.output_dir / job_id).exists()
            for path in settings.state_root.rglob("*"):
                if path.is_file():
                    assert b"offline.example/video" not in path.read_bytes()
                    assert b"result.mp4" not in path.read_bytes()


def test_ttl_starts_at_completion_and_missing_files_have_no_links(monkeypatch, offline):
    import app.storage as module
    clock = [1_800_000_000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module, "utcnow", lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    monkeypatch.setattr(settings, "persist_storage", False)
    with TestClient(app) as client:
        job_id = submit(client)
        detail = finish(client, job_id)
        completed = datetime.fromisoformat(detail["completed_at"]).timestamp()
        assert datetime.fromisoformat(detail["expires_at"]).timestamp() == completed + 3600
        link = detail["url"]
        clock[0] += 3601
        assert client.get(link).status_code == 403  # The short link also expired.
        expired = client.get("/api/jobs/" + job_id).json()
        assert expired["status"] == "done" and expired["availability"] == "expired"
        assert "url" not in expired
        client.portal.call(queue.cleanup)
        assert not (settings.temp_storage_dir / "jobs" / job_id).exists()
        # Correct processing and physical availability are different states.
        clock[0] += settings.temp_history_ttl_seconds + 1
        client.portal.call(queue.cleanup)
        assert client.get("/api/jobs/" + job_id).status_code == 404


def test_expired_file_rejects_still_valid_signature(monkeypatch, offline):
    monkeypatch.setattr(settings, "persist_storage", False)
    monkeypatch.setattr(settings, "temp_file_ttl_seconds", 1)
    with TestClient(app) as client:
        job_id = submit(client)
        detail = finish(client, job_id)
        job = queue.histories[1].get(job_id, (next(queue.histories[1].scan()).owner,))
        job.expires_at = "2000-01-01T00:00:00+00:00"
        queue.persist(job)
        response = client.get(detail["url"])
        assert response.status_code == 410 and response.json()["error"]["code"] == "file.expired"


def test_cookie_is_private_and_ownership_applies_to_every_job_route(offline):
    with TestClient(app) as client:
        response = client.get("/api/session")
        cookie_a = response.cookies[COOKIE]
        assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
        job_id = submit(client)
        client.portal.call(queue.pending.join)
        client.cookies.clear()
        response = client.get("/api/session")
        assert response.cookies[COOKIE] != cookie_a
        assert client.get("/api/jobs").json()["items"] == []
        for path, method in (("/api/jobs/" + job_id, "GET"), ("/api/jobs/" + job_id, "DELETE")):
            foreign = client.request(method, path)
            missing = client.request(method, "/api/jobs/nonexistent")
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json()
        assert client.get("/api/jobs/" + job_id, headers=ADMIN).status_code == 404  # Admin is not a global identity.
        own = client.get("/api/jobs/" + job_id, headers={"Cookie": f"{COOKIE}={cookie_a}"}).json()
        assert own["availability"] == "available"
        # Explicitly documented bearer-capability semantics.
        assert client.get(own["url"]).status_code == 200
        assert client.get(own["url"].replace("token=", "token=bad")).status_code == 403


def test_public_capabilities_and_admin_only_operations(offline):
    with TestClient(app) as client:
        capabilities = client.get("/api/session").json()
        assert capabilities["accounts_enabled"] is False
        assert not {"output_dir", "llm_base_url", "api_key"} & capabilities.keys()
        assert "models" not in client.get("/api/health").json()
        for method, path, payload in (("GET", "/api/settings", None), ("PUT", "/api/settings", {}),
                                      ("POST", "/api/models/whisper", {}), ("POST", "/api/models/demucs", {}),
                                      ("POST", "/api/transcribe", {"url": "https://offline.example/video"})):
            assert client.request(method, path, json=payload).status_code == 403
        result = client.put("/api/settings", json={"persist_storage": False, "anon_download_limit": 0}, headers=ADMIN)
        assert result.status_code == 200
        assert settings.persist_storage is True and settings.anon_download_limit == 5
        assert "api_key" not in result.json()


def test_csrf_and_foreign_origins_are_rejected_before_inspection(monkeypatch):
    monkeypatch.setattr(ytdlp, "info", lambda _: pytest.fail("No inspection before CSRF checks"))
    with TestClient(app) as client:
        assert client.post("/api/download", json={"url": "https://example.com"}, headers={"X-Muvio-Request": ""}).status_code == 403
        assert client.post("/api/download", json={"url": "https://example.com"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_range_head_and_error_paths_release_leases(offline):
    with TestClient(app) as client:
        detail = finish(client, submit(client))
        response = client.get(detail["url"], headers={"Range": "bytes=2-5"})
        assert response.status_code == 206 and response.content == b"2345"
        assert client.head(detail["url"]).status_code == 200
        assert client.get(detail["url"], headers={"Range": "bytes=999-"}).status_code == 416
        assert client.get(detail["url"], headers={"Range": "bytes=invalid"}).status_code == 400
        assert not storage.leases and not storage.owners


def test_cleanup_waits_for_existing_transfer_and_rejects_new_ones(monkeypatch):
    monkeypatch.setattr(settings, "persist_storage", False)

    async def verify():
        local = Queue()
        await local.start()
        try:
            async def operation(job):
                job.output_path = str(write_result(storage.folder(job)))
            job = local.submit("download", "offline", operation, owner="one")
            await local.pending.join()
            path = storage.path(job, job.output_path)
            storage.acquire(job)
            response = LeasedFileResponse(job, path)
            started, release = asyncio.Event(), asyncio.Event()

            async def send(message):
                if message["type"] == "http.response.body":
                    started.set()
                    await release.wait()

            async def receive():
                return {"type": "http.disconnect"}

            task = asyncio.create_task(response({"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}, receive, send))
            event_wait = asyncio.create_task(started.wait())
            done, _ = await asyncio.wait({event_wait, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            assert event_wait in done
            job.expires_at = "2000-01-01T00:00:00+00:00"
            local.persist(job)
            await local.cleanup()
            assert path.exists()
            with pytest.raises(AppError) as exc:
                storage.acquire(job)
            assert exc.value.code == "file.expired"
            release.set()
            await task
            assert not storage.leases
            await local.cleanup()
            assert not path.exists()
        finally:
            await local.stop()

    asyncio.run(verify())


@pytest.mark.parametrize("failure", ["disconnect", "timeout"])
def test_transfer_send_failures_and_timeout_release_leases(monkeypatch, failure):
    monkeypatch.setattr(settings, "transfer_timeout_seconds", 1)

    async def verify():
        local = Queue()
        await local.start()
        try:
            async def operation(job):
                job.output_path = str(write_result(storage.folder(job)))
            job = local.submit("download", "offline", operation, owner="one")
            await local.pending.join()
            storage.acquire(job)

            async def send(_):
                if failure == "disconnect":
                    raise OSError("disconnected")
                # A controlled cancellation scope triggers the timeout path.
                raise TimeoutError("slow client")

            with pytest.raises((OSError, TimeoutError)):
                await LeasedFileResponse(job, storage.path(job, job.output_path))({"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}, None, send)
            assert not storage.leases and not storage.owners
        finally:
            await local.stop()

    asyncio.run(verify())


def test_restart_restores_temporary_metadata_without_resetting_quota(monkeypatch, offline):
    monkeypatch.setattr(settings, "persist_storage", False)
    with TestClient(app) as client:
        job_id = submit(client)
        detail = finish(client, job_id)
        cookie = client.cookies.get(COOKIE)
        assert client.get("/api/session").json()["quota"]["remaining"] == 4
    with TestClient(app) as client:
        client.cookies.set(COOKIE, cookie)
        restored = client.get("/api/jobs/" + job_id).json()
        assert restored["expires_at"] == detail["expires_at"] and restored["availability"] == "available"
        assert client.get("/api/session").json()["quota"]["remaining"] == 4
        storage.path(next(queue.histories[1].scan()), "result.mp4").unlink()
        restored = client.get("/api/jobs/" + job_id).json()
        assert restored["availability"] == "missing" and "url" not in restored


def test_changing_mode_or_temporary_root_never_reinterprets_old_jobs(monkeypatch, offline):
    with TestClient(app) as client:
        persistent_id = submit(client)
        finish(client, persistent_id)
        cookie = client.cookies.get(COOKIE)
    monkeypatch.setattr(settings, "persist_storage", False)
    with TestClient(app) as client:
        client.cookies.set(COOKIE, cookie)
        temporary_id = submit(client)
        temporary = finish(client, temporary_id)
        assert client.get("/api/jobs/" + persistent_id).json()["storage_mode"] == "persistent"
        assert (settings.output_dir / persistent_id / "result.mp4").is_file()
    old_root = settings.temp_storage_dir
    monkeypatch.setattr(settings, "temp_storage_dir", old_root.with_name("other-temporary"))
    with TestClient(app) as client:
        client.cookies.set(COOKIE, cookie)
        assert client.get("/api/jobs/" + temporary_id).json()["expires_at"] == temporary["expires_at"]
        assert client.get(temporary["url"]).status_code == 200


def test_legacy_migration_is_backed_up_and_administrative_only():
    settings.state_root.mkdir(parents=True)
    folder = settings.output_dir / "legacy"
    folder.mkdir(parents=True)
    result = write_result(folder)
    old = {"id": "legacy", "kind": "download", "url": "https://old.example", "status": "done", "output_path": str(result)}
    with sqlite3.connect(settings.state_root / "history.db") as conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("INSERT INTO jobs VALUES (?,?)", ("legacy", json.dumps(old)))
    with TestClient(app) as client:
        assert client.get("/api/jobs").json()["items"] == []
        admin = client.get("/api/jobs", headers=ADMIN).json()["items"]
        assert [j["id"] for j in admin] == ["legacy"]
        assert admin[0]["availability"] == "available"
        assert list(settings.state_root.glob("history.db.v0-*.bak"))
        assert result.exists()


def test_job_pagination_is_bounded_and_owner_scoped(offline):
    with TestClient(app) as client:
        for _ in range(4):
            finish(client, submit(client))
        first = client.get("/api/jobs?limit=2").json()
        second = client.get("/api/jobs", params={"limit": 2, "cursor": first["next_cursor"]}).json()
        assert len(first["items"]) == len(second["items"]) == 2
        assert not {j["id"] for j in first["items"]} & {j["id"] for j in second["items"]}
        assert second["next_cursor"] is None
        assert client.get("/api/jobs?limit=999").status_code == 422
        assert client.get("/api/jobs?cursor=not-base64").status_code == 422


def test_event_buffers_are_bounded_and_owner_filtered():
    async def verify():
        local = Queue()
        a, b = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=1)
        local.subscribers = {a: "a", b: "b"}
        for index in range(20):
            local.publish(Job(str(index), "download", owner="a"))
        assert a.qsize() == 1 and b.empty()
        assert (await a.get())["id"] == "19"
    asyncio.run(verify())


def test_cancel_waits_for_thread_writer_before_removing_workspace(monkeypatch):
    async def verify():
        local = Queue()
        await local.start()
        entered, release = threading.Event(), threading.Event()
        try:
            def writer(job):
                folder = storage.folder(job)
                entered.set()
                assert release.wait(5)
                assert folder.exists(), "Workspace deleted while a thread still writes"
                write_result(folder)

            async def operation(job):
                await run_sync(writer, job)

            job = local.submit("download", "offline", operation, owner="a")
            await run_sync(entered.wait, 5)
            await local.cancel(job)
            assert storage.folder(job).exists()
            release.set()
            await local.pending.join()
            assert not storage.folder(job).exists()
            assert job.status == "cancelled"
        finally:
            release.set()
            await local.stop()
    asyncio.run(verify())


@pytest.mark.parametrize("root", ["filesystem", "state", "models", "inside-state"])
def test_dangerous_cleanup_roots_are_rejected(monkeypatch, root):
    value = {"filesystem": Path("/"), "state": settings.state_root, "models": settings.models_dir,
             "inside-state": settings.state_root / "temporary"}[root]
    monkeypatch.setattr(settings, "temp_storage_dir", value)
    with pytest.raises(ValueError):
        storage.start()


def test_symlinks_and_traversal_cannot_escape_workspaces(tmp_path):
    storage.start()
    job = Job("a" * 32, "download", owner="one")
    storage.assign(job)
    folder = storage.create(job)
    target = tmp_path / "private"
    target.write_text("private")
    (folder / "escape").symlink_to(target)
    for path in ("../private", "nested/../../private", "escape", str(target)):
        with pytest.raises(ValueError):
            storage.path(job, path)
    assert storage.delete(job) and target.read_text() == "private"


def test_cleanup_retries_failures_and_ignores_unmanaged_files(monkeypatch):
    async def verify():
        local = Queue()
        await local.start()
        try:
            foreign = settings.temp_storage_dir / "jobs" / ("f" * 32)
            foreign.mkdir()
            (foreign / "unrelated.txt").write_text("keep")
            job = local.prepare("download", "offline", owner="a")
            job.status, job.availability = "error", "missing"
            local.persist(job)
            local.jobs.pop(job.id)
            original = storage.delete
            calls = []

            def failing(value):
                calls.append(value.id)
                raise OSError("disk unavailable")

            monkeypatch.setattr(storage, "delete", failing)
            await local.cleanup()
            assert storage.folder(job).exists()
            monkeypatch.setattr(storage, "delete", original)
            await local.cleanup()
            assert not storage.folder(job).exists() and calls == [job.id]
            assert (foreign / "unrelated.txt").read_text() == "keep"
        finally:
            await local.stop()
    asyncio.run(verify())


@pytest.mark.parametrize("field,value", [("anon_download_limit", -1), ("temp_file_ttl_seconds", 0),
                                         ("temp_cleanup_interval_seconds", 0), ("anon_download_window_seconds", 0)])
def test_configuration_validation(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_quota_reservations_are_atomic_under_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "anon_download_limit", 5)
    monkeypatch.setattr(settings, "max_jobs_per_owner", 20)
    admission.start()
    actor = Principal("same-session")
    barrier = threading.Barrier(12)

    def attempt(index):
        barrier.wait()
        try:
            return admission.reserve(actor, str(index), {"kind": "download"})[0]
        except AppError as exc:
            assert exc.code == "quota.exceeded" and exc.status_code == 429
            assert int(exc.headers["Retry-After"]) > 0
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(attempt, range(12)))
    assert len({r for r in results if r}) == 5
    assert admission.quota(actor)["remaining"] == 0


def test_zero_quota_means_unlimited_but_does_not_disable_capacity(monkeypatch):
    monkeypatch.setattr(settings, "anon_download_limit", 0)
    monkeypatch.setattr(settings, "max_jobs_per_owner", 1)
    admission.start()
    actor = Principal("one")
    assert admission.quota(actor)["remaining"] is None
    admission.reserve(actor, "one", {})
    with pytest.raises(AppError) as exc:
        admission.reserve(actor, "two", {})
    assert exc.value.code == "capacity.exceeded"


def test_real_rolling_window_survives_restart_and_releases_prestart_reservations(monkeypatch):
    import app.admission as module
    clock = [1_800_000_000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    admission.start()
    actor = Principal("one")
    first, _ = admission.reserve(actor, "first", {})
    admission.accepted(first)
    admission.started(first)
    admission.finish(first, True)
    clock[0] += 3600
    second, _ = admission.reserve(actor, "second", {})
    admission.accepted(second)
    admission.started(second)
    admission.finish(second, True)
    queued, _ = admission.reserve(actor, "never-started", {})
    admission.accepted(queued)
    assert admission.quota(actor)["remaining"] == 2
    admission.start()
    assert admission.quota(actor)["remaining"] == 3
    clock[0] += settings.anon_download_window_seconds - 3599
    assert admission.quota(actor)["remaining"] == 4  # Second use is still inside its own window.


def test_idempotent_api_retries_do_not_duplicate_or_charge_twice(offline):
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "same-intent"}
        first = submit(client, headers=headers)
        finish(client, first)
        assert submit(client, headers=headers) == first
        assert len(client.get("/api/jobs").json()["items"]) == 1
        assert client.get("/api/session").json()["quota"]["remaining"] == 4
        conflict = client.post("/api/download", json={"url": "https://offline.example/other"}, headers=headers)
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency.conflict"


def test_pending_idempotent_reservation_is_not_admitted_twice():
    admission.start()
    actor = Principal("one")
    job_id, replay = admission.reserve(actor, "same", {"url": "sensitive-url"})
    assert not replay
    with pytest.raises(AppError) as exc:
        admission.reserve(actor, "same", {"url": "sensitive-url"})
    assert exc.value.code == "admission.pending"
    admission.accepted(job_id)
    assert admission.reserve(actor, "same", {"url": "sensitive-url"}) == (job_id, True)
    assert b"sensitive-url" not in admission.db.read_bytes()


def test_selectors_and_validation_failures_release_quota(offline):
    with TestClient(app) as client:
        offline["entries"] = [{"url": "https://offline.example/one"}]
        assert client.post("/api/download", json={"url": "https://offline.example/list"}).json()["status"] == "picker"
        assert client.get("/api/session").json()["quota"]["remaining"] == 5
        offline["entries"], offline["is_live"] = [], True
        assert client.post("/api/download", json={"url": "https://offline.example/live"}).status_code == 400
        assert client.get("/api/session").json()["quota"]["remaining"] == 5


def test_quota_errors_include_public_retry_information(monkeypatch, offline):
    monkeypatch.setattr(settings, "anon_download_limit", 1)
    with TestClient(app) as client:
        finish(client, submit(client))
        response = client.post("/api/download", json={"url": "https://offline.example/again"})
        assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
        error = response.json()["error"]
        assert error["code"] == "quota.exceeded"
        assert error["context"]["remaining"] == 0 and error["context"]["limit"] == 1


def test_quota_store_failure_is_fail_closed(monkeypatch, offline):
    with TestClient(app, raise_server_exceptions=False) as client:
        def broken():
            raise sqlite3.OperationalError("unavailable")
        monkeypatch.setattr(admission, "connect", broken)
        assert client.post("/api/download", json={"url": "https://offline.example/video"}).status_code == 500
        assert not queue.jobs


@pytest.mark.parametrize("kind", ["subtitle", "upload", "transcribe-url", "studio"])
def test_all_media_flows_use_private_temporary_workspace_and_clean_intermediates(monkeypatch, offline, kind):
    from app.transcription import engine, separate
    monkeypatch.setattr(settings, "persist_storage", False)
    seen = []

    def transcribe(source, *_):
        seen.append(source)
        assert source.is_relative_to(settings.temp_storage_dir)
        return {"language": "en", "duration": 1, "segments": [{"start": 0, "end": 1, "text": "Hello", "confidence": 1, "words": []}]}

    async def probe(_):
        return {"format": {"duration": "1"}}

    async def preprocess(_source, target, *_):
        return write_result(target.parent, target.name)

    def stem(_source, workspace):
        assert workspace.is_relative_to(settings.temp_storage_dir)
        workspace.mkdir()
        return write_result(workspace, "vocals.wav")

    from app.engine import ffmpeg
    monkeypatch.setattr(engine, "transcribe", transcribe)
    monkeypatch.setattr(ffmpeg, "probe", probe)
    monkeypatch.setattr(ffmpeg, "preprocess", preprocess)
    monkeypatch.setattr(separate, "separate", stem)
    with TestClient(app) as client:
        if kind == "subtitle":
            response = client.post("/api/subtitle-download", json={"url": "https://offline.example/video", "lang": "en"})
        elif kind in {"upload", "studio"}:
            response = client.post("/api/transcribe", headers=ADMIN, files={"file": ("private.wav", b"offline audio", "audio/wav")},
                                   data={"options": json.dumps({"mode": "studio" if kind == "studio" else "quick", "separateVocals": kind == "studio"})})
        else:
            response = client.post("/api/transcribe", headers=ADMIN, json={"url": "https://offline.example/video"})
        assert response.status_code == 200, response.text
        job_id = response.json()["output"]["job_id"]
        detail = finish(client, job_id)
        assert detail["status"] == "done" and detail["availability"] == "available", detail
        folder = settings.temp_storage_dir / "jobs" / job_id
        assert not (folder / "upload.wav").exists() and not (folder / "prepared.wav").exists()
        assert not (folder / "stems").exists()
        assert not (settings.state_root / "stems").exists()
        assert not (settings.output_dir / job_id).exists()
        if kind != "subtitle":
            assert set(detail["downloads"]) == {"srt", "vtt", "txt", "json"}
            assert seen
        for path in settings.state_root.rglob("*"):
            if path.is_file():
                assert b"private.wav" not in path.read_bytes()
                assert b"offline.example/video" not in path.read_bytes()


def test_upload_retries_hash_actual_content_and_do_not_duplicate_jobs(monkeypatch, offline):
    from app.engine import ffmpeg
    from app.transcription import engine

    async def probe(_):
        return {"format": {"duration": "1"}}

    monkeypatch.setattr(ffmpeg, "probe", probe)
    monkeypatch.setattr(engine, "transcribe", lambda *_: {"segments": [], "duration": 1, "language": "en"})
    with TestClient(app) as client:
        arguments = {"headers": ADMIN | {"Idempotency-Key": "upload-retry"}, "files": {"file": ("song.wav", b"same bytes", "audio/wav")}}
        first = client.post("/api/transcribe", **arguments)
        assert first.status_code == 200
        finish(client, first.json()["job_id"])
        second = client.post("/api/transcribe", **arguments)
        assert second.json()["job_id"] == first.json()["job_id"]
        arguments["files"] = {"file": ("song.wav", b"different bytes", "audio/wav")}
        assert client.post("/api/transcribe", **arguments).status_code == 409
        assert len(client.get("/api/jobs").json()["items"]) == 1


def test_upload_canceled_before_worker_is_cleaned_immediately(monkeypatch, offline):
    from app.api import routes
    from app.engine import ffmpeg
    monkeypatch.setattr(settings, "persist_storage", False)
    monkeypatch.setattr(settings, "max_concurrent_jobs", 1)

    async def probe(_):
        return {"format": {"duration": "1"}}

    monkeypatch.setattr(ffmpeg, "probe", probe)
    with TestClient(app) as client:
        gate = client.portal.call(asyncio.Event)

        async def blocking(job, _):
            await gate.wait()
            job.output_path = str(write_result(storage.folder(job)))

        monkeypatch.setattr(routes, "download_operation", blocking)
        submit(client)
        response = client.post("/api/transcribe", headers=ADMIN, files={"file": ("song.wav", b"audio", "audio/wav")})
        job_id = response.json()["job_id"]
        folder = settings.temp_storage_dir / "jobs" / job_id
        assert (folder / "upload.wav").exists()
        cancelled = client.delete("/api/jobs/" + job_id)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert not folder.exists()
        client.portal.call(gate.set)
        client.portal.call(queue.pending.join)


def test_lost_container_temporary_files_do_not_restore_links_or_reset_quota(monkeypatch, offline):
    monkeypatch.setattr(settings, "persist_storage", False)
    with TestClient(app) as client:
        job_id = submit(client)
        finish(client, job_id)
        cookie = client.cookies.get(COOKIE)
    # Only this test's disposable media root is removed, simulating recreation.
    shutil.rmtree(settings.temp_storage_dir)
    with TestClient(app) as client:
        client.cookies.set(COOKIE, cookie)
        assert client.get("/api/jobs/" + job_id).status_code == 404
        assert client.get("/api/session").json()["quota"]["remaining"] == 4


def test_crashed_jobs_are_reconciled_and_charged_only_if_started(monkeypatch):
    monkeypatch.setattr(settings, "persist_storage", False)
    storage.start()
    admission.start()
    history = History(settings.temp_storage_dir / "history.db")
    history.start()
    actor = Principal("one")
    for started in (False, True):
        job_id, _ = admission.reserve(actor, str(started), {})
        job = Job(job_id, "transcribe", owner=actor.owner, status="running" if started else "queued")
        storage.assign(job)
        storage.create(job)
        write_result(storage.folder(job), "unfinished.part")
        history.save(job)
        admission.accepted(job_id)
        if started:
            admission.started(job_id)
    with TestClient(app) as client:
        assert all(j.status == "error" for j in queue.histories[1].scan())
        assert all(not storage.folder(j).exists() for j in queue.histories[1].scan())
        assert admission.quota(actor)["remaining"] == 4


def test_actual_timeout_cancellation_releases_file_lease(monkeypatch):
    import app.storage as module
    original = module.anyio.fail_after
    monkeypatch.setattr(module.anyio, "fail_after", lambda _: original(0))

    async def verify():
        local = Queue()
        await local.start()
        try:
            async def operation(job):
                job.output_path = str(write_result(storage.folder(job)))
            job = local.submit("download", "offline", operation, owner="one")
            await local.pending.join()
            storage.acquire(job)

            async def send(_):
                await asyncio.Event().wait()

            with pytest.raises(TimeoutError):
                await LeasedFileResponse(job, storage.path(job, job.output_path))({"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}, None, send)
            assert not storage.leases
        finally:
            await local.stop()
    asyncio.run(verify())


def test_second_application_process_is_refused():
    from main import single_instance
    with single_instance():
        with pytest.raises(RuntimeError, match="one process"):
            with single_instance():
                pytest.fail("A second worker must not start")


def test_simultaneous_api_submissions_cannot_overrun_quota(monkeypatch, offline):
    monkeypatch.setattr(settings, "max_jobs_per_owner", 20)
    monkeypatch.setattr(settings, "max_metadata_requests", 8)
    with TestClient(app) as client:
        client.get("/api/session")
        barrier = threading.Barrier(12)

        def request(index):
            barrier.wait()
            return client.post("/api/download", json={"url": "https://offline.example/video"}, headers={"Idempotency-Key": str(index)})

        with ThreadPoolExecutor(max_workers=12) as executor:
            responses = list(executor.map(request, range(12)))
        assert sum(r.status_code == 200 for r in responses) == 5
        assert sum(r.status_code == 429 for r in responses) == 7
        client.portal.call(queue.pending.join)
        assert len(client.get("/api/jobs").json()["items"]) == 5


def test_request_body_limit_applies_to_chunked_uploads_before_spooling(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    with TestClient(app) as client:
        response = client.post("/api/transcribe", headers=ADMIN | {"Content-Type": "application/json"},
                               content=iter([b"x" * (1024 * 1024), b"x" * (1024 * 1024 + 1)]))
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "capacity.exceeded"


def test_failures_after_start_consume_quota_and_clean_files(monkeypatch, offline):
    def failure(_url, folder, *_):
        write_result(folder, "unfinished.part")
        raise AppError("media.unavailable", "Controlled offline failure")
    monkeypatch.setattr(ytdlp, "download", failure)
    with TestClient(app) as client:
        job_id = submit(client)
        result = finish(client, job_id)
        assert result["status"] == "error"
        assert not (settings.output_dir / job_id).exists()
        assert client.get("/api/session").json()["quota"]["remaining"] == 4


def test_storage_capacity_remains_enforced_with_unlimited_downloads(monkeypatch, offline):
    monkeypatch.setattr(settings, "anon_download_limit", 0)
    monkeypatch.setattr(settings, "max_storage_mb", 1)
    with TestClient(app) as client:
        response = client.post("/api/download", json={"url": "https://offline.example/video"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "capacity.exceeded"
        assert client.get("/api/session").json()["quota"]["remaining"] is None
        assert client.get("/api/jobs").json()["items"] == []


def test_internal_admission_failure_cleans_workspace_and_blocks_new_jobs(monkeypatch, offline):
    with TestClient(app, raise_server_exceptions=False) as client:
        def broken(_):
            raise sqlite3.OperationalError("controlled state outage")
        original = admission.accepted
        monkeypatch.setattr(admission, "accepted", broken)
        response = client.post("/api/download", json={"url": "https://offline.example/video"})
        assert response.status_code == 500
        assert not queue.jobs and not queue.healthy
        assert not [p for p in settings.output_dir.iterdir() if p.is_dir()]
        monkeypatch.setattr(admission, "accepted", original)
        assert client.post("/api/download", json={"url": "https://offline.example/retry"}).status_code == 503


def test_failed_quota_finalization_does_not_kill_worker_or_hang_queue(monkeypatch, offline):
    with TestClient(app) as client:
        def broken(*_):
            raise sqlite3.OperationalError("controlled quota outage")
        monkeypatch.setattr(admission, "finish", broken)
        job_id = submit(client)
        finish(client, job_id)
        assert not queue.healthy
        assert all(not worker.done() for worker in queue.workers)
        assert client.post("/api/download", json={"url": "https://offline.example/retry"}).status_code == 503

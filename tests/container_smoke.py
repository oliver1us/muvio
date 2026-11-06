"""Run via docker stdin, without mounting host data or allowing network access.

docker run --rm -i --network none -e API_KEY=offline-container-key \
  -e PERSIST_STORAGE=false muvio-persistence-check:local python - < tests/container_smoke.py
"""
import os

from fastapi.testclient import TestClient

from app.config import settings
from app.engine import ytdlp
from app.queue.jobs import queue
from app.storage import storage
from main import app

assert os.getuid() == 10001, "The image must run as muvio, not root"
settings.max_job_mb = 1
settings.min_free_disk_mb = 1
stream = {"format_id": "combined", "vcodec": "avc1.64001f", "acodec": "mp4a.40.2", "ext": "mp4", "height": 720}
ytdlp.info = lambda _: {"entries": [], "is_live": False, "duration": 1, "formats": [stream], "videoStreams": [stream], "audioStreams": [stream], "subtitles": []}


def offline_download(_url, folder, *_):
    (folder / "intermediate.part").write_bytes(b"partial")
    result = folder / "container-result.mp4"
    result.write_bytes(b"0123456789")
    return result


ytdlp.download = offline_download
with TestClient(app, headers={"X-Muvio-Request": "1"}) as client:
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/session").json()["quota"]["remaining"] == 5
    response = client.post("/api/download", json={"url": "https://offline.invalid/container"})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    client.portal.call(queue.pending.join)
    job = client.get("/api/jobs/" + job_id).json()
    assert job["status"] == "done", job
    assert job["storage_mode"] == ("persistent" if settings.persist_storage else "temporary")
    root = settings.output_dir if settings.persist_storage else settings.temp_storage_dir / "jobs"
    assert (root / job_id / "container-result.mp4").is_file()
    assert not (root / job_id / "intermediate.part").exists()
    assert client.get(job["url"], headers={"Range": "bytes=2-5"}).content == b"2345"
    assert not storage.leases
    if not settings.persist_storage:
        assert not (settings.output_dir / job_id).exists()
        assert b"offline.invalid" not in (settings.state_root / "history.db").read_bytes()
    assert client.get("/api/session").json()["quota"]["remaining"] == 4
    client.cookies.clear()
    assert client.get("/api/jobs").json()["items"] == []
    assert client.get("/api/jobs/" + job_id).status_code == 404
    assert client.get("/api/settings").status_code == 403
    assert client.get("/api/settings", headers={"Authorization": "Api-Key " + os.environ["API_KEY"]}).status_code == 200
print("Container smoke OK: UID 10001, " + ("persistent" if settings.persist_storage else "temporary") + " storage, isolation, quota and Range")

"""All tests use disposable roots, never the operator's actual data directory."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings


@pytest.fixture(autouse=True)
def isolated_roots(monkeypatch, tmp_path):
    # Neither a developer's .env nor saved preferences may steer offline tests.
    for name, field in Settings.model_fields.items():
        monkeypatch.setattr(settings, name, field.default)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "state" / "downloads")
    monkeypatch.setattr(settings, "state_dir", tmp_path / "state")
    monkeypatch.setattr(settings, "models_dir", tmp_path / "state" / "models")
    monkeypatch.setattr(settings, "temp_storage_dir", tmp_path / "temporary")
    monkeypatch.setattr(settings, "api_key", "offline-admin-key")
    monkeypatch.setattr(settings, "api_key_required", False)
    monkeypatch.setattr(settings, "rate_limit_max", 1000)
    monkeypatch.setattr(settings, "max_job_mb", 4)
    monkeypatch.setattr(settings, "min_free_disk_mb", 1)
    from main import _requests
    _requests.clear()
    original = TestClient.__init__

    def initialize(self, *args, **kwargs):
        kwargs["headers"] = {"X-Muvio-Request": "1", **kwargs.get("headers", {})}
        original(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", initialize)

"""Runtime configuration and atomic settings persistence."""
import json
import logging
import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment defaults overridden by a local settings file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    output_dir: Path = Path("data/downloads")
    models_dir: Path = Path("data/models")
    state_dir: Path | None = None
    persist_storage: bool = True
    temp_storage_dir: Path = Path("/tmp/muvio")
    temp_file_ttl_seconds: int = Field(default=3600, gt=0)
    temp_cleanup_interval_seconds: int = Field(default=60, gt=0)
    temp_history_ttl_seconds: int = Field(default=86400, gt=0)
    anon_download_limit: int = Field(default=5, ge=0)
    anon_download_window_seconds: int = Field(default=86400, gt=0)
    session_max_age_seconds: int = Field(default=2592000, gt=0)
    cookie_secure: bool = False
    public_origin: str = ""
    max_concurrent_jobs: int = Field(default=3, gt=0, le=32)
    max_pending_jobs: int = Field(default=20, gt=0)
    max_jobs_per_owner: int = Field(default=3, gt=0)
    max_metadata_requests: int = Field(default=4, gt=0)
    max_job_mb: int = Field(default=1024, gt=0)
    max_storage_mb: int = Field(default=10240, gt=0)
    min_free_disk_mb: int = Field(default=512, gt=0)
    max_media_duration_seconds: int = Field(default=7200, gt=0)
    job_timeout_seconds: int = Field(default=3600, gt=0)
    transfer_timeout_seconds: int = Field(default=3600, gt=0)
    max_transfers: int = Field(default=8, gt=0)
    max_transfers_per_owner: int = Field(default=2, gt=0)
    max_event_subscribers: int = Field(default=100, gt=0)
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    demucs_enabled: bool = False
    demucs_model: str = "mdx_extra"
    ffmpeg_path: str = "ffmpeg"
    max_upload_mb: int = Field(default=200, gt=0)
    rate_limit_max: int = Field(default=10, gt=0)
    rate_limit_window: int = Field(default=60, gt=0)
    api_key_required: bool = False
    api_key: str = ""
    cors_allow_origin: str = ""
    signing_secret: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_provider: str = "openai-compatible"
    enable_llm_cleanup: bool = False

    @property
    def state_root(self) -> Path:
        """Keep the historical state location unless explicitly configured."""
        return self.state_dir or self.output_dir.parent

    @model_validator(mode="after")
    def validate_policy(self):
        if self.session_max_age_seconds < self.anon_download_window_seconds + self.temp_file_ttl_seconds:
            raise ValueError("Session lifetime must cover the quota window and result TTL")
        if self.public_origin and not self.public_origin.startswith(("http://", "https://")):
            raise ValueError("PUBLIC_ORIGIN must be an HTTP(S) origin")
        return self


settings = Settings()
EDITABLE = {"max_concurrent_jobs", "whisper_model", "whisper_device", "whisper_compute_type", "demucs_enabled", "demucs_model", "llm_base_url", "llm_api_key", "llm_model", "llm_provider", "enable_llm_cleanup"}


def load_settings() -> None:
    """Restore local preferences, backing up a damaged configuration."""
    CONFIG_PATH = settings.state_root / "config.json"
    if not CONFIG_PATH.exists():
        return
    try:
        values = json.loads(CONFIG_PATH.read_text())
        validated = Settings(**(settings.model_dump() | {k: v for k, v in values.items() if k in EDITABLE}))
        for key in EDITABLE:
            setattr(settings, key, getattr(validated, key))
    except (ValueError, OSError) as exc:
        logging.warning("Invalid local settings: %s", exc)
        os.replace(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))


def save_settings(values: dict) -> dict:
    """Validate and persist editable settings without exposing secrets."""
    CONFIG_PATH = settings.state_root / "config.json"
    validated = Settings(**(settings.model_dump() | {k: v for k, v in values.items() if k in EDITABLE}))
    for key in EDITABLE:
        setattr(settings, key, getattr(validated, key))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps({k: getattr(settings, k) for k in EDITABLE}))
    os.replace(temp, CONFIG_PATH)
    return admin_settings()


def admin_settings() -> dict:
    """Return installation settings to an administrator, without secrets."""
    return {k: str(v) if isinstance(v, Path) else v for k, v in settings.model_dump().items() if k not in {"api_key", "llm_api_key", "signing_secret"}} | {"llm_key_is_set": bool(settings.llm_api_key)}


def public_settings() -> dict:
    """Only expose capabilities needed by visitors, never server paths/providers."""
    return {"persist_storage": settings.persist_storage, "temp_file_ttl_seconds": settings.temp_file_ttl_seconds,
            "accounts_enabled": False, "transcription_admin_only": True}

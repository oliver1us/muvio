"""Runtime configuration and atomic settings persistence."""
import json
import logging
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment defaults overridden by a local settings file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    output_dir: Path = Path("data/downloads")
    models_dir: Path = Path("data/models")
    max_concurrent_jobs: int = 3
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    demucs_enabled: bool = False
    demucs_model: str = "mdx_extra"
    ffmpeg_path: str = "ffmpeg"
    max_upload_mb: int = 200
    rate_limit_max: int = 10
    rate_limit_window: int = 60
    api_key_required: bool = False
    api_key: str = ""
    cors_allow_origin: str = ""
    signing_secret: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_provider: str = "openai-compatible"
    enable_llm_cleanup: bool = False


settings = Settings()
CONFIG_PATH = settings.output_dir.parent / "config.json"
EDITABLE = {"max_concurrent_jobs", "whisper_model", "whisper_device", "whisper_compute_type", "demucs_enabled", "demucs_model", "llm_base_url", "llm_api_key", "llm_model", "llm_provider", "enable_llm_cleanup"}


def load_settings() -> None:
    """Restore local preferences, backing up a damaged configuration."""
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
    validated = Settings(**(settings.model_dump() | {k: v for k, v in values.items() if k in EDITABLE}))
    for key in EDITABLE:
        setattr(settings, key, getattr(validated, key))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps({k: getattr(settings, k) for k in EDITABLE}))
    os.replace(temp, CONFIG_PATH)
    return public_settings()


def public_settings() -> dict:
    """Return settings without credentials."""
    return {k: str(v) if isinstance(v, Path) else v for k, v in settings.model_dump().items() if k not in {"api_key", "llm_api_key", "signing_secret"}} | {"llm_key_is_set": bool(settings.llm_api_key)}

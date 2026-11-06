"""Optional Demucs separation, invoked only after explicit installation."""
import importlib.util
import os
from pathlib import Path

from app.config import settings
from app.errors import AppError


def model_marker() -> Path:
    """Locate the explicit-download marker for the selected model."""
    from app.engine.naming import sanitize
    return settings.models_dir / ("demucs-" + sanitize(settings.demucs_model) + ".ready")


def download_model() -> str:
    """Download Demucs weights only when explicitly requested by the user."""
    if importlib.util.find_spec("demucs") is None:
        raise AppError("demucs.failed", "Install the optional demucs package before downloading its model.")
    try:
        os.environ["TORCH_HOME"] = str(settings.models_dir / "torch")
        from demucs.pretrained import get_model
        get_model(settings.demucs_model)
        model_marker().parent.mkdir(parents=True, exist_ok=True)
        model_marker().touch()
        return settings.demucs_model
    except Exception as exc:
        raise AppError("demucs.failed", "Could not download the Demucs model: " + str(exc)[:200]) from exc


def separate(source: Path, workspace: Path) -> Path | None:
    """Separate within a private job, never cache media across identities."""
    if not settings.demucs_enabled or importlib.util.find_spec("demucs") is None or not model_marker().exists():
        return None
    os.environ["TORCH_HOME"] = str(settings.models_dir / "torch")
    output = workspace
    stem = output / settings.demucs_model / source.stem / "vocals.wav"
    if stem.exists():
        return stem
    try:
        from demucs.separate import main
        output.mkdir(parents=True, exist_ok=True)
        main(["-n", settings.demucs_model, "--two-stems", "vocals", "-o", str(output), str(source)])
    except Exception as exc:
        raise AppError("demucs.failed", "Vocal separation failed: " + str(exc)[:240]) from exc
    if not stem.exists():
        raise AppError("demucs.failed", "Vocal separation produced no vocal stem.")
    return stem

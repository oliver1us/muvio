"""Optional Demucs separation, invoked only after explicit installation."""
import hashlib
import importlib.util
from pathlib import Path

from app.config import settings
from app.errors import AppError


def separate(source: Path, cache: Path) -> Path | None:
    """Return a cached vocal stem; return None if Demucs is not installed."""
    if not settings.demucs_enabled or importlib.util.find_spec("demucs") is None:
        return None
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    output = cache / digest.hexdigest()
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

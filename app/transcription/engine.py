"""Lazy, serialized faster-whisper inference."""
import threading
from pathlib import Path

from app.config import settings
from app.errors import AppError

_lock = threading.Lock()
_model = None
_model_name = ""


def model_available() -> bool:
    """Check installation without importing the heavy inference runtime."""
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def download_model() -> str:
    """Explicitly download the configured model to local storage."""
    from faster_whisper import WhisperModel
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    WhisperModel(settings.whisper_model, device="cpu", compute_type="int8", download_root=str(settings.models_dir))
    return settings.whisper_model


def transcribe(path: Path, language: str | None = None, studio: bool = False, prompt: str = "") -> dict:
    """Run word-level ASR using only a model already on disk."""
    global _model, _model_name
    with _lock:
        if not model_available():
            raise AppError("whisper.disabled", "Install faster-whisper to enable transcription.")
        try:
            from faster_whisper import WhisperModel
            if _model is None or _model_name != settings.whisper_model:
                settings.models_dir.mkdir(parents=True, exist_ok=True)
                _model = WhisperModel(settings.whisper_model, device=settings.whisper_device, compute_type=settings.whisper_compute_type, download_root=str(settings.models_dir), local_files_only=True)
                _model_name = settings.whisper_model
            segments, info = _model.transcribe(str(path), language=language, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500}, word_timestamps=True, beam_size=8 if studio else 5, temperature=0, condition_on_previous_text=False, initial_prompt=prompt or None, no_speech_threshold=0.6, compression_ratio_threshold=2.4, log_prob_threshold=-1.0)
            rows = [{"start": s.start, "end": s.end, "text": s.text.strip(), "confidence": sum(w.probability for w in (s.words or [])) / max(len(s.words or []), 1), "words": [{"word": w.word, "start": w.start, "end": w.end, "confidence": w.probability} for w in (s.words or [])]} for s in segments]
            return {"language": info.language, "duration": info.duration, "segments": rows}
        except AppError:
            raise
        except Exception as exc:
            reason = str(exc).lower()
            if any(needle in reason for needle in ("not found", "cannot find", "offline", "local_files_only", "cached snapshot")):
                raise AppError("whisper.model_missing", "Download the Whisper model explicitly in Settings before transcribing.") from exc
            raise AppError("whisper.failed", "Transcription failed: " + str(exc)[:250]) from exc

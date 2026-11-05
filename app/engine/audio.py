"""Source audio languages, resolved before codec and bitrate preferences."""
import re

from app.errors import AppError


def language(stream: dict) -> str:
    """Keep regional and descriptive tracks distinct; never guess missing tags."""
    return (stream.get("language") or "und").replace("_", "-").lower()


def is_original(stream: dict) -> bool:
    """yt-dlp marks YouTube originals with preference 10 and/or an original note."""
    note = stream.get("note") or stream.get("format_note") or ""
    return (stream.get("language_preference") or 0) >= 10 or bool(re.search(r"\boriginal\b", note, re.I))


def audio_tracks(formats: list[dict]) -> list[dict]:
    """Group encodings by language, putting confirmed originals first."""
    tracks = {}
    for stream in formats:
        if stream.get("acodec") in (None, "none") or not stream.get("format_id"):
            continue
        code = language(stream)
        track = tracks.setdefault(code, {"language": code, "is_original": False})
        track["is_original"] |= is_original(stream)
    # Do not use regional source defaults or input order when original metadata
    # is absent. The UI explicitly identifies this deterministic fallback.
    return sorted(tracks.values(), key=lambda track: (
        not track["is_original"], track["language"].endswith("-desc"),
        track["language"] == "und", track["language"],
    ))


def resolve_language(formats: list[dict], requested: str = "original") -> str | None:
    """Resolve automatic once; explicit languages must exist without fallback."""
    tracks = audio_tracks(formats)
    if requested == "original":
        return tracks[0]["language"] if tracks else None
    code = requested.replace("_", "-").lower()
    if not any(track["language"] == code for track in tracks):
        raise AppError("format.unavailable", "The selected audio language is no longer available. Refresh the link and choose another audio track.")
    return code


def audio_rank(stream: dict) -> tuple:
    """Prefer original audio, then quality, with a stable format-ID tie-breaker."""
    return (is_original(stream), stream.get("abr") or stream.get("tbr") or 0,
            stream.get("asr") or 0, str(stream["format_id"]))


def choose_audio(formats: list[dict], code: str, output_format: str) -> dict:
    """Select an exact format ID within the already resolved language."""
    candidates = [stream for stream in formats if stream.get("format_id") and
                  stream.get("acodec") not in (None, "none") and language(stream) == code]
    if not candidates:
        raise AppError("format.unavailable", "The selected audio language is no longer available.")
    return max(candidates, key=lambda stream: (
        is_original(stream), stream.get("vcodec") in (None, "none"),
        output_format == "m4a" and (stream.get("acodec") or "").startswith(("mp4a", "aac")),
        *audio_rank(stream)[1:],
    ))

"""Safe file names and unique output paths."""
import re
from pathlib import Path


def sanitize(name: str) -> str:
    """Remove filesystem metacharacters while retaining Unicode."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180].rstrip(" .") or "media"


def unique(path: Path) -> Path:
    """Avoid overwriting an existing output file."""
    candidate = path
    number = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


def filename(style: str, metadata: dict, ext: str) -> str:
    """Render a safe basename for basic, pretty, or classic naming."""
    title = metadata.get("title") or "media"
    artist = metadata.get("artist") or metadata.get("uploader") or "Unknown artist"
    item_id = metadata.get("id") or "media"
    stems = {"basic": title, "pretty": f"{artist} - {title}", "classic": f"{artist} - {title} [{item_id}]"}
    suffix = "." + sanitize(ext.lstrip("."))
    stem = sanitize(stems.get(style, title))[:180 - len(suffix)].rstrip(" .")
    return (stem or "media") + suffix

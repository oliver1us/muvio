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

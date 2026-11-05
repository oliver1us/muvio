"""Local subtitle conversions."""
import re


def vtt_to_srt(text: str) -> str:
    """Convert WebVTT timing cues to numbered SubRip cues."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.lstrip("\ufeff")):
        lines = block.strip().splitlines()
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if index is None:
            continue
        times = lines[index].split("-->")
        def stamp(value: str) -> str:
            value = value.strip().split()[0].replace(".", ",")
            return ("00:" + value) if value.count(":") == 1 else value
        cues.append(f"{len(cues)+1}\n{stamp(times[0])} --> {stamp(times[1])}\n" + "\n".join(lines[index+1:]))
    return "\n\n".join(cues) + "\n"

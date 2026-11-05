"""Studio dual-pass merge and exports."""
import json
import re
from pathlib import Path


def clean(rows: list[dict]) -> list[dict]:
    """Discard silent, punctuation-only, and immediately repeated segments."""
    result = []
    for row in sorted(rows, key=lambda r: r["start"]):
        text = row["text"].strip()
        if not re.search(r"\w", text, re.UNICODE) or row["end"] <= row["start"]:
            continue
        if result and text.casefold() == result[-1]["text"].strip().casefold():
            continue
        result.append(row)
    return result


def merge(original: list[dict], vocals: list[dict]) -> list[dict]:
    """Align by time and choose the higher confidence overlapping segment."""
    rows = list(original)
    for vocal in vocals:
        if vocal.get("confidence", 0) < 0.35:
            continue
        matches = [(i, row) for i, row in enumerate(rows) if max(0, min(row["end"], vocal["end"]) - max(row["start"], vocal["start"])) > 0.5 * (vocal["end"] - vocal["start"])]
        if matches:
            index, row = max(matches, key=lambda pair: min(pair[1]["end"], vocal["end"]) - max(pair[1]["start"], vocal["start"]))
            if vocal["confidence"] > row.get("confidence", 0):
                rows[index] = vocal
        else:
            rows.append(vocal)
    return clean(rows)


def timestamp(seconds: float, vtt: bool = False) -> str:
    """Render millisecond-resolution subtitle time."""
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{'.' if vtt else ','}{millis:03}"


def export(transcript: dict, folder: Path, title: str) -> dict[str, Path]:
    """Write TXT, SRT, VTT and word-timed JSON beside the source."""
    from app.engine.naming import sanitize
    rows = clean(transcript["segments"])
    stem = sanitize(title) + "." + sanitize(transcript["language"] or "unknown") + ".transcript"
    paths = {ext: folder / f"{stem}.{ext}" for ext in ("txt", "srt", "vtt", "json")}
    paths["txt"].write_text("\n".join(r["text"] for r in rows) + "\n", encoding="utf-8")
    for ext in ("srt", "vtt"):
        body = "\n\n".join(f"{i}\n{timestamp(r['start'], ext == 'vtt')} --> {timestamp(r['end'], ext == 'vtt')}\n{r['text']}" for i, r in enumerate(rows, 1))
        paths[ext].write_text(("WEBVTT\n\n" if ext == "vtt" else "") + body + "\n", encoding="utf-8")
    paths["json"].write_text(json.dumps(transcript | {"segments": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths

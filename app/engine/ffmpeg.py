"""Asynchronous FFmpeg operations."""
import asyncio
import json
import shutil
from pathlib import Path

from app.config import settings
from app.errors import AppError


def available() -> bool:
    """Check whether an FFmpeg executable is on the path."""
    return bool(shutil.which(settings.ffmpeg_path))


async def run(*args: str, timeout: float = 3600) -> bytes:
    """Run a media command without a shell or event-loop blocking."""
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise AppError("ffmpeg.failed", "Media processing timed out.") from exc
        if process.returncode:
            raise AppError("ffmpeg.failed", "Media processing failed: " + stderr.decode(errors="replace")[-1200:])
        return stdout
    except FileNotFoundError as exc:
        raise AppError("ffmpeg.missing", "FFmpeg is not installed.") from exc


async def extract_audio(source: Path, target: Path, codec: str = "wav", bitrate: str | None = None) -> Path:
    """Extract audio to the chosen container."""
    encoders = {"m4a": "aac", "opus": "libopus", "wav": "pcm_s16le", "mp3": "libmp3lame", "flac": "flac", "ogg": "libvorbis"}
    args = [settings.ffmpeg_path, "-y", "-i", str(source), "-map_metadata", "0", "-vn", "-c:a", encoders[codec]]
    if bitrate and codec not in {"wav", "flac"}:
        args += ["-b:a", bitrate + "k"]
    await run(*(args + [str(target)]))
    return target


async def attach_cover(source: Path, image: Path, target: Path) -> Path:
    """Attach an image to M4A audio after conversion without changing the audio."""
    await run(settings.ffmpeg_path, "-y", "-i", str(source), "-i", str(image), "-map", "0:a", "-map", "1:v:0", "-c:a", "copy", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic", str(target))
    return target


async def preprocess(source: Path, target: Path, focus: bool = False) -> Path:
    """Normalize audio for studio transcription."""
    filters = "highpass=f=250,lowpass=f=8000,equalizer=f=2000:t=q:w=1.2:g=4," if focus else "highpass=f=300,"
    await run(settings.ffmpeg_path, "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-af", filters + "loudnorm=I=-16:TP=-1.5:LRA=11", str(target))
    return target


async def embed_subtitle(video: Path, subtitle: Path, target: Path) -> Path:
    """Mux a subtitle into a video without re-encoding video or audio."""
    if video.suffix == ".webm":
        raise AppError("format.unavailable", "WebM cannot contain SRT/VTT text subtitles; choose MKV or MP4.")
    codec = "mov_text" if target.suffix == ".mp4" else "srt"
    await run(settings.ffmpeg_path, "-y", "-i", str(video), "-i", str(subtitle), "-map", "0", "-map", "1:0", "-c", "copy", "-c:s", codec, str(target))
    return target


async def probe(path: Path) -> dict:
    """Inspect a local media file."""
    result = await run("ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path))
    return json.loads(result)

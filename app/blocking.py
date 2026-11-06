"""Do not abandon a media-writing thread when its async caller is cancelled."""
import asyncio

import anyio


async def run_sync(function, *args):
    task = asyncio.create_task(anyio.to_thread.run_sync(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # yt-dlp hooks observe job.cancelled. Whisper/Demucs may need to finish;
        # the workspace must not be deleted while that thread is still writing.
        try:
            await task
        except Exception:
            pass
        raise

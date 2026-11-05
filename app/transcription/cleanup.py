"""Opt-in transcript correction with strict line preservation."""
import httpx

from app.config import settings
from app.errors import AppError


async def cleanup(lines: list[str], title: str, artist: str) -> tuple[list[str], str | None]:
    """Correct words with an explicitly configured endpoint, or retain raw text."""
    if not settings.enable_llm_cleanup or not settings.llm_base_url or not settings.llm_model or (settings.llm_provider != "ollama" and not settings.llm_api_key):
        return lines, "llm.not_configured: Configure and enable an AI provider first."
    system = "Correct misheard words only. Do not rewrite, summarize, translate, add or remove content. Keep exactly the same number of lines and preserve timing markers exactly. If uncertain, retain the original. Output corrected text only."
    prompt = f"Title: {title}\nArtist: {artist}\n\n" + "\n".join(lines)
    base = settings.llm_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            if settings.llm_provider == "anthropic":
                response = await client.post(base + "/v1/messages", headers={"x-api-key": settings.llm_api_key, "anthropic-version": "2023-06-01"}, json={"model": settings.llm_model, "max_tokens": 4096, "system": system, "messages": [{"role": "user", "content": prompt}]})
                response.raise_for_status()
                result = response.json()["content"][0]["text"]
            elif settings.llm_provider == "ollama":
                response = await client.post(base + "/api/chat", json={"model": settings.llm_model, "stream": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
                response.raise_for_status()
                result = response.json()["message"]["content"]
            else:
                response = await client.post(base + "/chat/completions", headers={"Authorization": "Bearer " + settings.llm_api_key}, json={"model": settings.llm_model, "temperature": 0, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
                response.raise_for_status()
                result = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return lines, "llm.failed: The cleanup provider failed (" + type(exc).__name__ + ")."
    corrected = result.strip().splitlines()
    if len(corrected) != len(lines):
        return lines, "llm.line_count_mismatch: Provider changed the line count; original retained."
    return corrected, None

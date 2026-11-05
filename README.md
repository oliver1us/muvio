# muvio

Self-hosted, dark, minimal downloader and local transcription interface. Supports public media sources handled by yt-dlp. No accounts, ads, or analytics.

## Run

Docker: `docker compose up --build` and visit http://localhost:3838. Docker installs FFmpeg and Python dependencies. Transcription uses CPU by default; a GPU needs a custom CTranslate2/CUDA image.

Local (Python 3.11+, FFmpeg installed): `python3 -m venv .venv`, `. .venv/bin/activate`, `pip install -r requirements.txt`, `uvicorn main:app --host 127.0.0.1 --port 3838`. To enable vocal separation, run `pip install demucs` and enable it in Settings. From the project root, start with the single uvicorn command after dependencies are installed.

To use Whisper, click **Download Whisper model** in Settings; until then transcription returns `whisper.model_missing`. To use optional Demucs, install the package, then explicitly download its weights in Settings. Model downloads require explicit action; inference does not initiate a model download. Models and downloads remain under `data/`. No paid provider is needed. Optional AI cleanup is opt-in and sends transcript text to the configured endpoint; local Ollama is supported.

Configuration is read from `.env` and preferences from `data/config.json`. Copy `.env.example` to `.env` to customize output directories, models, workers, FFmpeg, upload limit, signing secret, CORS, optional auth, and optional LLM. Set `API_KEY_REQUIRED=true` and a long random `API_KEY` before exposing the app; enter the key in the settings drawer. Reverse-proxy through HTTPS (for example Caddy: `your.domain { reverse_proxy localhost:3838 }`). Keep `SIGNING_SECRET` fixed if download links must survive restarts; links expire after 15 minutes. File URLs work with their signature, even when API-key auth is enabled.

The API lives at `/api` and OpenAPI docs at `/docs`. POST `/api/download` returns `local-processing` with `output.job_id`, or `picker` for a collection. Poll `/api/jobs/{id}` for fresh signed download links. GET `/api/events` streams progress. POST `/api/transcribe` accepts JSON `{ "url": "...", "mode": "quick" }` or multipart `file` with JSON `options`. Transcripts are exported as TXT, SRT, VTT, and word-timed JSON. All errors use `{ "status": "error", "error": { "code": "...", "context": { "message": "..." } } }`.

The UI uses plain ES modules and serves `app/web/css/custom.css` directly. Jost is bundled locally under `app/web/fonts/` with its SIL Open Font License. No Node, CSS build, font service, or runtime CDN is needed. Edit `custom.css` to change the design.

Downloads start with automatic settings: video targets the highest available resolution at or below 720p (or the lowest available if all exceed it), preferring MP4/H.264 with AAC at that resolution. If needed, another available combination is shown by its actual format. Audio defaults to MP3 at 320 kbps. Advanced controls expose only compatible source combinations; an explicitly selected resolution must exist for that combination. Video is not re-encoded. A larger audio bitrate or lossless output cannot recover quality already lost at the source.

Video and audio downloads prefer the source's identified **original audio language**, before bitrate or container preferences. Each card shows the selected language and offers the available language tracks (including regional variants). If the source does not identify the original or the language, the card says so instead of guessing. Unmarked tracks use a stable language-code order, not the server region's default. Only existing source audio is offered; this does not translate or generate a dub. An explicitly selected language never falls back to another language, including when a queued download refreshes its metadata. `/api/info` exposes `audioTracks`, `defaultAudioLanguage`, and language-specific `videoOptions`; `/api/download` accepts `audioLanguage` (default `"original"`, or an available language tag such as `"en"` or `"es-419"`). Audio language is separate from subtitles and Whisper's recognition language.

Offline checks: `python -m pytest`. Pure frontend selection tests: `node --test tests/test_ui.mjs`. Optional real-browser checks use Playwright: `pip install playwright`, `python -m playwright install chromium`, then `python -m pytest tests/test_browser.py`. Browser tests use mocked media and do not download from media websites.

**Responsible use:** Save only media you have permission to download. Respect copyright and platform terms; do not redistribute others' work. No DRM circumvention or private-content access is implemented. Public deployments should add TLS and further network isolation for untrusted URLs.

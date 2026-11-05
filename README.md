# muvio

Self-hosted, dark, minimal downloader and local transcription interface. Supports public media sources handled by yt-dlp. No accounts, ads, or analytics.

## Run

Docker: `docker compose up --build` and visit http://localhost:3838. Docker installs FFmpeg and Python dependencies. Transcription uses CPU by default; a GPU needs a custom CTranslate2/CUDA image.

Local (Python 3.11+, FFmpeg installed): `python3 -m venv .venv`, `. .venv/bin/activate`, `pip install -r requirements.txt`, `uvicorn main:app --host 127.0.0.1 --port 3838`. To enable vocal separation, run `pip install demucs` and enable it in Settings. From the project root, start with the single uvicorn command after dependencies are installed.

To use Whisper, click **Download Whisper model now** in Settings; until then transcription returns `whisper.model_missing`. To use optional Demucs, install the package, then explicitly download its weights in Settings. Model downloads require explicit action; inference does not initiate a model download. Models and downloads remain under `data/`. No paid provider is needed. Optional AI cleanup is opt-in and sends transcript text to the configured endpoint; local Ollama is supported.

Configuration is read from `.env` and preferences from `data/config.json`. Copy `.env.example` to `.env` to customize output directories, models, workers, FFmpeg, upload limit, signing secret, CORS, optional auth, and optional LLM. Set `API_KEY_REQUIRED=true` and a long random `API_KEY` before exposing the app; enter the key in the settings drawer. Reverse-proxy through HTTPS (for example Caddy: `your.domain { reverse_proxy localhost:3838 }`). Keep `SIGNING_SECRET` fixed if download links must survive restarts; links expire after 15 minutes. File URLs work with their signature, even when API-key auth is enabled.

The API lives at `/api` and OpenAPI docs at `/docs`. POST `/api/download` returns `local-processing` with `output.job_id`, or `picker` for a collection. Poll `/api/jobs/{id}` for fresh signed download links. GET `/api/events` streams progress. POST `/api/transcribe` accepts JSON `{ "url": "...", "mode": "quick" }` or multipart `file` with JSON `options`. Transcripts are exported as TXT, SRT, VTT, and word-timed JSON. All errors use `{ "status": "error", "error": { "code": "...", "context": { "message": "..." } } }`.

The UI uses plain ES modules and one checked-in CSS file built from Tailwind and `app/web/css/custom.css`. No Node or runtime CDN is needed. To change styles, edit `custom.css` and rebuild with the [standalone Tailwind CLI](https://tailwindcss.com/blog/standalone-cli): `tailwindcss -i app/web/css/source.css -o app/web/css/app.css --minify`.

**Responsible use:** Save only media you have permission to download. Respect copyright and platform terms; do not redistribute others' work. No DRM circumvention or private-content access is implemented. Public deployments should add TLS and further network isolation for untrusted URLs.

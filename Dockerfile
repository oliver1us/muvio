FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 muvio
COPY --from=builder /install /usr/local
WORKDIR /app
COPY main.py ./
COPY app/ ./app/
RUN mkdir -p /app/data && chown -R muvio:muvio /app/data
USER muvio
ENV OUTPUT_DIR=/app/data/downloads MODELS_DIR=/app/data/models PYTHONUNBUFFERED=1
VOLUME /app/data
EXPOSE 3838
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3838/api/health',timeout=3)" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3838"]

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-download.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/download_service.py /app/download_service.py

RUN useradd -m pikaraoke && mkdir -p /songs && chown pikaraoke:pikaraoke /songs
USER pikaraoke

ENV DOWNLOAD_PATH=/songs/
ENV YTDLP_PATH=/usr/local/bin/yt-dlp
ENV SERVICE_PORT=5003

EXPOSE 5003

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5003/health'); r.raise_for_status()"

CMD ["python", "download_service.py"]

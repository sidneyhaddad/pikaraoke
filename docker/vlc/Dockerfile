FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    vlc \
    ffmpeg \
    unzip \
    libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

# VLC refuses to run as root — create a non-root user
RUN useradd -m pikaraoke
ENV HOME=/home/pikaraoke

WORKDIR /app

COPY requirements-vlc.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY lib/ /app/lib/
COPY services/vlc_service.py /app/vlc_service.py

# VLC temp directory for pitch-shifted files
RUN mkdir -p /tmp/pikaraoke && chown pikaraoke:pikaraoke /tmp/pikaraoke

USER pikaraoke

ENV VLC_PORT=5002
ENV SERVICE_PORT=5010
ENV VLC_PATH=/usr/bin/vlc

EXPOSE 5010

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5010/health'); r.raise_for_status()"

CMD ["python", "vlc_service.py"]

FROM python:3.11-slim-bookworm

WORKDIR /app

# Install yt-dlp for version checking and fallback local downloads
COPY docker/web/requirements-web.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py karaoke.py constants.py /app/
COPY lib/ /app/lib/
COPY templates/ /app/templates/
COPY static/ /app/static/
COPY translations/ /app/translations/
COPY myvikaraoke.png /app/myvikaraoke.png
COPY babel.cfg messages.pot /app/

RUN mkdir -p /tmp/pikaraoke /songs

ENV DOCKER_MODE=true
ENV DOWNLOAD_PATH=/songs/
ENV VLC_SERVICE_URL=http://vlc:5010
ENV SPLASH_SERVICE_URL=http://splash:5004
ENV DOWNLOAD_SERVICE_URL=http://download:5003

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5000/'); r.raise_for_status()"

CMD ["python", "app.py", \
     "--download-path", "/songs/", \
     "--hide-splash-screen", \
     "-y", "/usr/local/bin/yt-dlp", \
     "--vlc-path", "/usr/bin/vlc", \
     "--vlc-port", "5002"]

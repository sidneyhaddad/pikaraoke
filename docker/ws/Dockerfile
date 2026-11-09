FROM python:3.11-slim-bookworm

WORKDIR /app

COPY docker/ws/requirements-ws.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# NOTE: The pikaraoke-ws source must be available in the build context.
# When building from the pikaraoke root, symlink or copy:
#   ln -s ../pikaraoke-ws pikaraoke-ws   (or adjust docker-compose context)
COPY pikaraoke-ws/server.py /app/server.py
COPY pikaraoke-ws/static/ /app/static/

RUN mkdir -p /songs

ENV PIKARAOKE_URL=http://web:5000
ENV DOWNLOAD_PATH=/songs/
ENV DB_PATH=/songs/karaoke.db
ENV YTDLP_PATH=/usr/local/bin/yt-dlp
ENV PORT=80

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:80/'); r.raise_for_status()" || exit 1

CMD ["python", "server.py", \
     "--pikaraoke-url", "http://web:5000", \
     "--download-path", "/songs/", \
     "--db-path", "/songs/karaoke.db", \
     "--port", "80"]

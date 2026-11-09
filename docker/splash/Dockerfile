FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsdl2-2.0-0 \
    libsdl2-image-2.0-0 \
    libsdl2-mixer-2.0-0 \
    libsdl2-ttf-2.0-0 \
    libfreetype6 \
    libportmidi0 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-splash.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/splash_service.py /app/splash_service.py
COPY myvikaraoke.png /app/myvikaraoke.png

RUN mkdir -p /tmp/pikaraoke

ENV SERVICE_PORT=5004
ENV LOGO_PATH=/app/myvikaraoke.png
ENV QR_URL=http://myvikaraoke.local
ENV HIDE_IP=false

EXPOSE 5004

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5004/health'); r.raise_for_status()"

CMD ["python", "splash_service.py"]

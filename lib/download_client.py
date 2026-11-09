"""
HTTP client for the download service.

Used by karaoke.py when running in Docker mode to delegate
YouTube downloads to the pikaraoke-download container.
"""

import logging
import os

import requests

DOWNLOAD_URL = os.environ.get("DOWNLOAD_SERVICE_URL", "http://localhost:5003")
TIMEOUT = 10  # seconds


def download(video_url, high_quality=False):
    """Start a download job. Returns {"job_id": ..., "status": "downloading"} or None."""
    try:
        r = requests.post(
            "%s/download" % DOWNLOAD_URL,
            json={"url": video_url, "high_quality": high_quality},
            timeout=TIMEOUT,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        logging.error("Download service error: %s" % e)
    return None


def get_job_status(job_id):
    """Get download job status. Returns {"status": "downloading"|"done"|"error"} or None."""
    try:
        r = requests.get("%s/status/%s" % (DOWNLOAD_URL, job_id), timeout=TIMEOUT)
        if r.ok:
            return r.json()
    except Exception as e:
        logging.error("Download service status error: %s" % e)
    return None


def search(query, karaoke=True):
    """Search YouTube. Returns list of {"title", "url", "id"} or empty list."""
    try:
        r = requests.post(
            "%s/search" % DOWNLOAD_URL,
            json={"query": query, "karaoke": karaoke},
            timeout=30,
        )
        if r.ok:
            return r.json().get("results", [])
    except Exception as e:
        logging.error("Download service search error: %s" % e)
    return []


def get_version():
    """Get yt-dlp version string."""
    try:
        r = requests.get("%s/version" % DOWNLOAD_URL, timeout=TIMEOUT)
        if r.ok:
            return r.json().get("version", "unknown")
    except Exception:
        pass
    return "unknown"


def upgrade():
    """Upgrade yt-dlp. Returns result dict or None."""
    try:
        r = requests.post("%s/upgrade" % DOWNLOAD_URL, timeout=120)
        if r.ok:
            return r.json()
    except Exception as e:
        logging.error("Download service upgrade error: %s" % e)
    return None


def is_available():
    try:
        r = requests.get("%s/health" % DOWNLOAD_URL, timeout=TIMEOUT)
        return r.ok
    except Exception:
        return False

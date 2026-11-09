"""
Download Service — standalone HTTP API for yt-dlp downloads.

Runs inside the pikaraoke-download container. Manages YouTube downloads
and yt-dlp upgrades.
"""

import json
import logging
import os
import subprocess
import threading
import uuid

from flask import Flask, jsonify, request

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG,
)

app = Flask(__name__)

DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/songs/")
YTDLP_PATH = os.environ.get("YTDLP_PATH", "/usr/local/bin/yt-dlp")
HIGH_QUALITY = os.environ.get("HIGH_QUALITY", "false").lower() == "true"
SERVICE_PORT = int(os.environ.get("SERVICE_PORT", 5003))

# Track active downloads: {job_id: {"status": "downloading"|"done"|"error", "url": ..., "filepath": ...}}
jobs = {}
jobs_lock = threading.Lock()


def _do_download(job_id, video_url, high_quality):
    """Background thread: download a video using yt-dlp."""
    dl_path = DOWNLOAD_PATH + "%(title)s---%(id)s.%(ext)s"
    file_quality = (
        "bestvideo[ext!=webm][height<=1080]+bestaudio[ext!=webm]/best[ext!=webm]"
        if high_quality
        else "mp4"
    )
    cmd = [YTDLP_PATH, "-f", file_quality, "--write-sub", "-o", dl_path, video_url]
    logging.info("Download cmd: %s" % " ".join(cmd))

    rc = subprocess.call(cmd)
    if rc != 0:
        logging.warning("First attempt failed (rc=%d), retrying..." % rc)
        rc = subprocess.call(cmd)

    with jobs_lock:
        if rc == 0:
            jobs[job_id]["status"] = "done"
            logging.info("Download complete: %s" % video_url)
        else:
            jobs[job_id]["status"] = "error"
            logging.error("Download failed: %s" % video_url)


@app.route("/health")
def health():
    try:
        version = subprocess.check_output([YTDLP_PATH, "--version"]).decode().strip()
    except Exception:
        version = "unknown"
    return jsonify({"status": "ok", "ytdlp_version": version})


@app.route("/download", methods=["POST"])
def download():
    """Start a download. Body: {"url": "...", "high_quality": false}"""
    data = request.get_json(force=True)
    video_url = data.get("url", "")
    high_quality = data.get("high_quality", HIGH_QUALITY)

    if not video_url:
        return jsonify({"error": "no url specified"}), 400

    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {"status": "downloading", "url": video_url}

    t = threading.Thread(target=_do_download, args=[job_id, video_url, high_quality])
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id, "status": "downloading"})


@app.route("/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/search", methods=["POST"])
def search():
    """Search YouTube. Body: {"query": "...", "karaoke": true}"""
    data = request.get_json(force=True)
    query = data.get("query", "")
    karaoke = data.get("karaoke", True)

    if not query:
        return jsonify({"error": "no query"}), 400

    if karaoke:
        query += " karaoke"

    num_results = 10
    yt_search = 'ytsearch%d:"%s"' % (num_results, query)
    cmd = [YTDLP_PATH, "-j", "--no-playlist", "--flat-playlist", yt_search]

    try:
        output = subprocess.check_output(cmd).decode("utf-8")
        results = []
        for line in output.split("\n"):
            if len(line) > 2:
                j = json.loads(line)
                if "title" in j and "url" in j:
                    results.append({
                        "title": j["title"],
                        "url": j["url"],
                        "id": j.get("id", ""),
                    })
        return jsonify({"results": results})
    except Exception as e:
        logging.error("Search error: %s" % e)
        return jsonify({"error": str(e)}), 500


@app.route("/upgrade", methods=["POST"])
def upgrade():
    """Upgrade yt-dlp."""
    try:
        output = subprocess.check_output(
            ["pip3", "install", "--upgrade", "yt_dlp"]
        ).decode("utf-8")
        version = subprocess.check_output([YTDLP_PATH, "--version"]).decode().strip()
        return jsonify({"status": "ok", "output": output, "version": version})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/version")
def version():
    try:
        v = subprocess.check_output([YTDLP_PATH, "--version"]).decode().strip()
        return jsonify({"version": v})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    if not DOWNLOAD_PATH.endswith("/"):
        DOWNLOAD_PATH += "/"
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    logging.info("Starting Download Service on port %d" % SERVICE_PORT)
    app.run(host="0.0.0.0", port=SERVICE_PORT, debug=False)

"""
VLC Service — standalone HTTP API wrapping VLC player + ffmpeg pitch shift.

Runs inside the pikaraoke-vlc container. Receives commands from the web
container and manages VLC processes locally.
"""

import logging
import os
import subprocess
import sys

from flask import Flask, jsonify, request

# Add parent dir so we can import lib/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.vlcclient import VLCClient, get_default_vlc_path
from lib.get_platform import get_platform

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG,
)

app = Flask(__name__)

vlc_port = int(os.environ.get("VLC_PORT", 5002))
vlc_path = os.environ.get("VLC_PATH", get_default_vlc_path(get_platform()))
service_port = int(os.environ.get("SERVICE_PORT", 5010))

client = VLCClient(port=vlc_port, path=vlc_path)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "vlc_path": vlc_path, "vlc_port": vlc_port})


@app.route("/play", methods=["POST"])
def play():
    """Play a file. Body: {"file": "/songs/...", "semitones": 0, "seek_to": null}"""
    data = request.get_json(force=True)
    file_path = data.get("file", "")
    semitones = data.get("semitones", 0)
    seek_to = data.get("seek_to", None)

    if not file_path:
        return jsonify({"error": "no file specified"}), 400

    if semitones != 0:
        client.play_file_transpose(file_path, semitones, seek_to=seek_to)
    else:
        client.play_file(file_path)
        if seek_to and seek_to > 0:
            import time
            time.sleep(1)  # wait for VLC to start
            client.seek(seek_to)

    return jsonify({"status": "ok"})


@app.route("/play_transposed", methods=["POST"])
def play_transposed():
    """Play a pre-processed transposed file at a seek position."""
    data = request.get_json(force=True)
    file_path = data.get("file", "")
    seek_to = data.get("seek_to", 0.0)

    if not file_path:
        return jsonify({"error": "no file specified"}), 400

    resolved = client.process_file(file_path)
    command = client.cmd_base + ["--start-time", "%.1f" % seek_to, resolved]
    client.kill()
    if client.process:
        try:
            client.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    import time
    time.sleep(0.3)
    client.process = subprocess.Popen(
        command, shell=False, stdin=subprocess.PIPE
    )
    client.set_startup_volume()
    return jsonify({"status": "ok"})


@app.route("/stop", methods=["POST"])
def stop():
    client.stop()
    return jsonify({"status": "ok"})


@app.route("/pause", methods=["POST"])
def pause():
    client.pause()
    return jsonify({"status": "ok"})


@app.route("/resume", methods=["POST"])
def resume():
    client.play()
    return jsonify({"status": "ok"})


@app.route("/kill", methods=["POST"])
def kill():
    client.kill()
    return jsonify({"status": "ok"})


@app.route("/restart", methods=["POST"])
def restart():
    client.restart()
    return jsonify({"status": "ok"})


@app.route("/seek", methods=["POST"])
def seek():
    data = request.get_json(force=True)
    seconds = data.get("seconds", 0)
    client.seek(seconds)
    return jsonify({"status": "ok"})


@app.route("/volume/up", methods=["POST"])
def vol_up():
    client.vol_up()
    return jsonify({"status": "ok"})


@app.route("/volume/down", methods=["POST"])
def vol_down():
    client.vol_down()
    return jsonify({"status": "ok"})


@app.route("/status")
def status():
    is_running = client.is_running()
    is_playing = False
    is_paused = False
    volume = 0
    position = 0.0
    duration = 0.0

    if is_running:
        try:
            is_playing = client.is_playing()
            is_paused = client.is_paused()
            volume = client.get_volume()
            position, duration = client.get_time_and_length()
        except Exception:
            pass

    return jsonify({
        "is_running": is_running,
        "is_playing": is_playing,
        "is_paused": is_paused,
        "is_transposing": client.is_transposing,
        "transpose_progress": client.transpose_progress,
        "volume": volume,
        "position": position,
        "duration": duration,
    })


if __name__ == "__main__":
    logging.info("Starting VLC Service on port %d" % service_port)
    app.run(host="0.0.0.0", port=service_port, debug=False)

"""
HTTP client for the splash screen service.

Used by karaoke.py when running in Docker mode to delegate
splash screen rendering to the pikaraoke-splash container.
"""

import logging
import os

import requests

SPLASH_URL = os.environ.get("SPLASH_SERVICE_URL", "http://localhost:5004")
TIMEOUT = 2  # seconds


def _post(endpoint, data=None):
    try:
        r = requests.post("%s%s" % (SPLASH_URL, endpoint), json=data or {}, timeout=TIMEOUT)
        return r.ok
    except Exception as e:
        logging.debug("Splash service call failed (%s): %s" % (endpoint, e))
        return False


def update_state(now_playing=None, now_playing_user=None, queue=None,
                 is_transposing=False, transpose_progress=0, transpose_semitones=0):
    data = {}
    if now_playing is not None:
        data["now_playing"] = now_playing
    if now_playing_user is not None:
        data["now_playing_user"] = now_playing_user
    if queue is not None:
        data["queue"] = queue
    data["is_transposing"] = is_transposing
    data["transpose_progress"] = transpose_progress
    data["transpose_semitones"] = transpose_semitones
    return _post("/update_state", data)


def show_next_song(title, user):
    return _post("/show_next_song", {"title": title, "user": user})


def show_toast(text, duration=5):
    return _post("/toast", {"text": text, "duration": duration})


def render_splash():
    return _post("/render_splash")


def is_available():
    try:
        r = requests.get("%s/health" % SPLASH_URL, timeout=TIMEOUT)
        return r.ok
    except Exception:
        return False

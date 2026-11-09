"""
Remote VLC Client — communicates with the VLC service container via HTTP.

Drop-in replacement for VLCClient when running in Docker mode.
The interface matches VLCClient so karaoke.py doesn't need to change.
"""

import logging
import os

import requests

VLC_SERVICE_URL = os.environ.get("VLC_SERVICE_URL", "http://vlc:5010")
TIMEOUT = 5


class VLCRemoteClient:
    """Proxy that forwards VLC operations to the vlc_service container."""

    def __init__(self, **kwargs):
        self.is_transposing = False
        self.transpose_progress = 0
        self.transpose_duration = 0.0
        self.process = None  # Compatibility: always None in remote mode

    def _post(self, endpoint, data=None):
        try:
            r = requests.post(
                "%s%s" % (VLC_SERVICE_URL, endpoint),
                json=data or {},
                timeout=TIMEOUT,
            )
            return r.ok
        except Exception as e:
            logging.debug("VLC service error (%s): %s" % (endpoint, e))
            return False

    def _get_status(self):
        try:
            r = requests.get("%s/status" % VLC_SERVICE_URL, timeout=TIMEOUT)
            if r.ok:
                data = r.json()
                self.is_transposing = data.get("is_transposing", False)
                self.transpose_progress = data.get("transpose_progress", 0)
                return data
        except Exception:
            pass
        return {}

    def play_file(self, file_path, additional_parameters=None):
        self._post("/play", {"file": file_path, "semitones": 0})

    def play_file_transpose(self, file_path, semitones, seek_to=None):
        self.is_transposing = True
        self.transpose_progress = 0
        self._post("/play", {
            "file": file_path,
            "semitones": semitones,
            "seek_to": seek_to,
        })

    def stop(self):
        self._post("/stop")

    def pause(self):
        self._post("/pause")

    def play(self):
        self._post("/resume")

    def kill(self):
        self._post("/kill")

    def restart(self):
        self._post("/restart")

    def seek(self, seconds):
        self._post("/seek", {"seconds": seconds})

    def vol_up(self):
        self._post("/volume/up")

    def vol_down(self):
        self._post("/volume/down")

    def command(self, cmd):
        # Not supported remotely — VLC marquee commands are local only
        pass

    def is_running(self):
        status = self._get_status()
        return status.get("is_running", False) or self.is_transposing

    def is_playing(self):
        status = self._get_status()
        return status.get("is_playing", False)

    def is_paused(self):
        status = self._get_status()
        return status.get("is_paused", False)

    def get_volume(self):
        status = self._get_status()
        return status.get("volume", 0)

    def get_time_and_length(self):
        status = self._get_status()
        return (
            status.get("position", 0.0),
            status.get("duration", 0.0),
        )

    def _get_current_time(self):
        status = self._get_status()
        return status.get("position", 0.0)

    def set_transposing_complete(self):
        self.is_transposing = False
        self.transpose_progress = 0

    def process_file(self, file_path):
        # In remote mode, the vlc_service handles file processing
        return file_path

import logging
import os
import re
import random
import shutil
import string
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from threading import Thread, Timer

def _parse_ffmpeg_progress_time(line):
    """Extract time in seconds from an ffmpeg ``-progress`` output line.

    With ``-progress pipe:1``, ffmpeg writes structured lines to stdout like::

        out_time_us=5000000
        out_time=00:00:05.000000
        progress=continue

    This parses ``out_time_us=<microseconds>`` and returns seconds.
    Returns None if the line is not an ``out_time_us`` line.
    """
    line = line.strip()
    if line.startswith("out_time_us="):
        try:
            us = int(line.split("=", 1)[1])
            if us >= 0:
                return us / 1_000_000.0
        except (ValueError, IndexError):
            pass
    return None

# Sentinel returned by _ffmpeg_pitch_shift when the operation was cancelled
# (killed by a newer transpose request). Thread-safe: each call gets its own return value.
_FFMPEG_CANCELLED = object()

import requests

from lib.get_platform import get_platform


def get_default_vlc_path(platform):
    if platform == "osx":
        return "/Applications/VLC.app/Contents/MacOS/VLC"
    elif platform == "windows":
        alt_vlc_path = r"C:\\Program Files (x86)\\VideoLAN\VLC\\vlc.exe"
        if os.path.isfile(alt_vlc_path):
            return alt_vlc_path
        else:
            return r"C:\Program Files\VideoLAN\VLC\vlc.exe"
    else:
        return "/usr/bin/vlc"

class VLCClient:
    def __init__(self, port=5002, path=None, qrcode=None, url=None):

        # HTTP remote control server
        self.http_password = "".join(
            [random.choice(string.ascii_letters + string.digits) for n in range(32)]
        )
        self.port = port
        self.http_endpoint = "http://localhost:%s/requests/status.xml" % self.port
        self.http_command_endpoint = self.http_endpoint + "?command="
        self.is_transposing = False

        self.qrcode = qrcode
        self.url = url

        # Handle vlc paths
        self.platform = get_platform()
        if path == None:
            self.path = get_default_vlc_path(self.platform)
        else:
            self.path = path

        # Determine tmp directories (for things like extracted cdg files)
        if self.platform == "windows":
            self.tmp_dir = os.path.expanduser(r"~\\AppData\\Local\\Temp\\pikaraoke\\")
        else:
            self.tmp_dir = "/tmp/pikaraoke/"

        # Set up command line args
        self.cmd_base = [
            self.path,
            "-f",
            "--play-and-exit",
            "--intf",
            "dummy",
            "--extraintf",
            "http",
            "--http-port",
            "%s" % self.port,
            "--http-password",
            self.http_password,
            "--no-embedded-video",
            "--no-keyboard-events",
            "--no-mouse-events",
            "--mouse-hide-timeout",
            "0",
            "--video-on-top",
            "--disable-screensaver",
            #"--no-autoscale",
            "--no-video-title",
            "--mouse-hide-timeout",
            "0",
        ]
        if self.platform == "osx":
            self.cmd_base += [
                "--no-macosx-show-playback-buttons",
                "--no-macosx-show-playmode-buttons",
                "--no-macosx-interfacestyle",
                "--macosx-nativefullscreenmode",
                "--macosx-continue-playback",
                "0",
            ]
        elif self.platform in ("raspberry_pi", "linux"):
            # Let VLC auto-detect the video output (Wayland or X11)
            pass
        if self.qrcode:# and self.url:
            self.cmd_base += self.get_marquee_cmd()

        logging.info("VLC command base: " + " ".join(self.cmd_base))

        self.volume_offset = 10
        # VLC HTTP volume scale: 256 = 100%. Every new VLC process is forced
        # to this level at playback start.
        self.startup_volume = 256
        self.process = None
        self._ffmpeg_proc = None
        self.transpose_progress = 0       # 0-100 percentage
        self.transpose_duration = 0.0     # total file duration in seconds
    
    def get_marquee_cmd(self):
        marquee_text = '\n'
        if self.url:
            marquee_text = f'Pikaraoke - connect at: \n{self.url}'
        return ["--sub-source", 'logo{file=%s,position=9,x=2,opacity=200}:marq{marquee="%s",position=9,x=38,color=0xFFFFFF,size=11,opacity=200}' % (self.qrcode, marquee_text)]

    def handle_zipped_cdg(self, file_path):
        extracted_dir = os.path.join(self.tmp_dir, "extracted")
        if (os.path.exists(extracted_dir)):
            shutil.rmtree(extracted_dir)
        os.makedirs(extracted_dir, exist_ok=True)
        # Use system unzip to support Deflate64 and other methods
        # that Python's zipfile module cannot handle.
        rc = subprocess.call(
            ["unzip", "-o", "-q", file_path, "-d", extracted_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if rc != 0:
            # Fallback to Python zipfile for standard compression
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extracted_dir)
        
        mp3_file = None
        cdg_file = None
        files = os.listdir(extracted_dir)
        for file in files:
            ext = os.path.splitext(file)[1]
            if ext.casefold() == ".mp3":
                mp3_file = file
            elif ext.casefold() == ".cdg":
                cdg_file = file
        
        if (mp3_file is not None) and (cdg_file is not None):
            if (os.path.splitext(mp3_file)[0] == os.path.splitext(cdg_file)[0] ):
                return os.path.join(extracted_dir, mp3_file)
            else:
                raise Exception("Zipped .mp3 file did not have a matching .cdg file: " + files)
        else: 
            raise Exception("No .mp3 or .cdg was found in the zip file: " + file_path)

    def handle_mp3_cdg(self, file_path):
        f = os.path.splitext(os.path.basename(file_path))[0]
        pattern= f +'.cdg'
        rule = re.compile(re.escape(pattern), re.IGNORECASE)
        p=os.path.dirname(file_path)       # get the path, not the filename
        for n in os.listdir(p):
            if rule.match(n):
                return(file_path)
        if (1):
            # we didn't return, so always raise the exception: assert might work better?
            raise Exception("No matching .cdg file found for: " + file_path)

    def process_file(self, file_path):
        file_extension = os.path.splitext(file_path)[1]
        if (file_extension.casefold() == ".zip"):
            return self.handle_zipped_cdg(file_path)
        elif (file_extension.casefold() == ".mp3"):
            return self.handle_mp3_cdg(file_path)
        else:
            return file_path

    def _apply_startup_volume(self):
        """Poll the freshly spawned VLC process and set its volume once the
        HTTP interface responds. Runs in a daemon thread so playback start
        is never delayed."""
        for _ in range(20):
            time.sleep(0.5)
            if self.process is None or self.process.poll() is not None:
                return
            try:
                requests.get(
                    self.http_command_endpoint + "volume&val=%d" % self.startup_volume,
                    auth=("", self.http_password),
                    timeout=2,
                )
                logging.debug("Startup volume set to %d/256" % self.startup_volume)
                return
            except requests.exceptions.RequestException:
                continue
        logging.warning("Could not set startup volume: VLC HTTP interface never came up")

    def set_startup_volume(self):
        Thread(target=self._apply_startup_volume, daemon=True).start()

    def play_file(self, file_path, additional_parameters=None):
        try: 
            file_path = self.process_file(file_path)
            if self.is_playing() or self.is_paused():
                logging.debug("VLC is currently playing, stopping track...")
                self.stop()
                # this pause prevents vlc http server from being borked after transpose
                time.sleep(0.2)
            if self.platform == "windows":
                file_path = r"{}".format(file_path)
            if additional_parameters == None:
                command = self.cmd_base + [file_path]
            else:
                command = self.cmd_base + additional_parameters + [file_path]
            logging.debug("VLC Command: %s" % command)
            self.process = subprocess.Popen(
                command, shell=(self.platform == "windows"), stdin=subprocess.PIPE
            )
            self.set_startup_volume()
        except Exception as e:
            logging.error("Playing file failed: " + str(e))

    def play_file_transpose(self, file_path, semitones, seek_to=None):
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.is_transposing = True
        self.transpose_progress = 0
        logging.debug("Transposing file by %s semitones..." % semitones)

        if seek_to is None:
            seek_to = 0.0

        # Stop VLC immediately so the user sees the progress overlay, not the old song
        self.kill()
        if self.process:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

        shifted_path = self._ffmpeg_pitch_shift(file_path, semitones)

        # If cancelled by a newer transpose request, do nothing — the newer thread handles everything
        if shifted_path is _FFMPEG_CANCELLED:
            logging.debug("Transpose cancelled by newer request, skipping")
            self.is_transposing = False
            return

        self.transpose_progress = 100

        if shifted_path:
            time.sleep(0.3)
            command = self.cmd_base + ["--start-time", "%.1f" % seek_to, shifted_path]
            logging.debug("VLC Command (ffmpeg-shifted, seek=%.1fs): %s" % (seek_to, command))
            self.process = subprocess.Popen(
                command, shell=(self.platform == "windows"), stdin=subprocess.PIPE
            )
            self.set_startup_volume()
        else:
            # Fallback: VLC scaletempo_pitch filter
            logging.debug("Falling back to VLC scaletempo_pitch")
            src_type = 1 if self.platform == "raspberry_pi" else 0
            params = [
                "--audio-filter", "scaletempo_pitch",
                "--pitch-shift", "%s" % semitones,
                "--speex-resampler-quality", "10",
                "--src-converter-type", "%s" % src_type,
                "--start-time", "%.1f" % seek_to,
            ]
            self.play_file(file_path, params)

        # Prevent is_running() from returning False while transposing starts
        s = Timer(2.0, self.set_transposing_complete)
        s.start()

    def _get_current_time(self):
        """Return current playback position in seconds, or 0 if unavailable."""
        try:
            if self.is_running():
                status = self.get_status()
                t = status.find("time")
                if t is not None:
                    return float(t.text)
        except Exception:
            pass
        return 0.0

    def get_time_and_length(self):
        """Return (position_seconds, duration_seconds) or (0, 0) if unavailable."""
        try:
            if self.is_running():
                status = self.get_status()
                t = status.find("time")
                l = status.find("length")
                return (
                    float(t.text) if t is not None else 0.0,
                    float(l.text) if l is not None else 0.0,
                )
        except Exception:
            pass
        return 0.0, 0.0

    def seek(self, seconds):
        """Seek to absolute position in seconds."""
        return self.command("seek&val=%d" % int(seconds))

    def _ffmpeg_pitch_shift(self, file_path, semitones):
        """High-quality pitch shift via ffmpeg asetrate+atempo. Returns output path or None."""
        try:
            if not shutil.which("ffmpeg"):
                logging.debug("ffmpeg not found, skipping high-quality pitch shift")
                return None

            # Cancel any previous ffmpeg pitch shift still running
            if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
                logging.debug("Cancelling previous ffmpeg pitch shift")
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait()

            resolved = self.process_file(file_path)
            ext = os.path.splitext(resolved)[1].lower()

            # asetrate changes the assumed sample rate (shifts pitch),
            # aresample brings it back to 44100, atempo corrects the speed.
            ratio = 2 ** (float(semitones) / 12.0)
            af = "asetrate=44100*%.6f,aresample=44100,atempo=%.6f" % (ratio, 1.0 / ratio)

            # -progress pipe:1 must come BEFORE the output file; placing it
            # after makes ffmpeg treat "pipe:1" as a second output and fail.
            if ext in ('.mp4', '.mkv', '.avi', '.webm', '.mov'):
                out_path = os.path.join(self.tmp_dir, "shifted" + ext)
                cmd = ["ffmpeg", "-y", "-progress", "pipe:1",
                       "-i", resolved,
                       "-vcodec", "copy", "-af", af, out_path]
            else:
                # Audio file (mp3 extracted from cdg zip, etc.)
                out_path = os.path.join(self.tmp_dir, "shifted.mp3")
                cmd = ["ffmpeg", "-y", "-progress", "pipe:1",
                       "-i", resolved,
                       "-af", af, "-q:a", "0", out_path]
                # Copy matching CDG so VLC finds it alongside the shifted mp3
                cdg_src = os.path.splitext(resolved)[0] + ".cdg"
                if os.path.exists(cdg_src):
                    shutil.copy2(cdg_src, os.path.join(self.tmp_dir, "shifted.cdg"))
            logging.debug("ffmpeg pitch shift cmd: " + " ".join(cmd))
            self._ffmpeg_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )

            # Read structured progress from stdout (line-based, won't block)
            duration = self.transpose_duration if self.transpose_duration > 0 else 1.0
            while self._ffmpeg_proc.poll() is None:
                line = self._ffmpeg_proc.stdout.readline().decode(errors="ignore")
                if line:
                    t = _parse_ffmpeg_progress_time(line)
                    if t is not None and duration > 0:
                        pct = (t / duration) * 100
                        self.transpose_progress = min(int(pct), 99)
            # Drain remaining stdout
            self._ffmpeg_proc.stdout.read()

            rc = self._ffmpeg_proc.returncode
            if rc == 0:
                logging.debug("ffmpeg pitch shift ok: " + out_path)
                return out_path
            elif rc < 0:
                # Killed by signal — cancelled by a newer transpose request
                logging.debug("ffmpeg pitch shift cancelled (rc=%d)" % rc)
                return _FFMPEG_CANCELLED
            else:
                logging.warning("ffmpeg pitch shift failed (rc=%d)" % rc)
                return None
        except Exception as e:
            logging.warning("ffmpeg pitch shift error: " + str(e))
            return None

    def set_transposing_complete(self):
        self.is_transposing = False
        self.transpose_progress = 0
        logging.debug("Transposing complete")

    def command(self, command):
        if self.is_running():
            url = self.http_command_endpoint + command
            request = requests.get(url, auth=("", self.http_password))
            return request
        else:
            logging.error("No active VLC process. Could not run command: " + command)

    def pause(self):
        return self.command("pl_pause")

    def play(self):
        return self.command("pl_play")

    def stop(self):
        try:
            return self.command("pl_stop")
        except:
            e = sys.exc_info()[0]
            logging.warn(
                "Track stop: server may have shut down before http return code received: %s"
                % e
            )
            return

    def restart(self):
        logging.info(self.command("seek&val=0"))
        self.play()
        return self.command("seek&val=0")

    def vol_up(self):
        return self.command("volume&val=%d" % (self.get_volume() + self.volume_offset))

    def vol_down(self):
        return self.command("volume&val=%d" % (self.get_volume() - self.volume_offset))

    def kill(self):
        try:
            self.process.kill()
        except (OSError, AttributeError) as e:
            print(e)
            return

    def is_running(self):
        return (
            self.process != None and self.process.poll() == None
        ) or self.is_transposing

    def is_playing(self):
        if self.is_running():
            status = self.get_status()
            state = status.find("state").text
            return state == "playing"
        else:
            return False

    def is_paused(self):
        if self.is_running():
            status = self.get_status()
            state = status.find("state").text
            return state == "paused"
        else:
            return False

    def get_volume(self):
        status = self.get_status()
        return int(status.find("volume").text)

    def get_status(self):
        url = self.http_endpoint
        request = requests.get(url, auth=("", self.http_password))
        return ET.fromstring(request.text)

    def run(self):
        try:
            while True:
                pass
        except KeyboardInterrupt:
            self.kill()


# if __name__ == "__main__":
#     k = VLCClient()
#     k.play_file("/path/to/file.mp4")
#     time.sleep(2)
#     k.pause()
#     k.vol_up()
#     k.vol_up()
#     time.sleep(2)
#     k.vol_down()
#     k.vol_down()
#     time.sleep(2)
#     k.play()
#     time.sleep(2)
#     k.stop()

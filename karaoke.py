import json
import logging
import os
import random
import socket
import subprocess
import sys
import threading
import time
import gettext
from subprocess import check_output
from pathlib import Path

from unidecode import unidecode
from constants import COLORS

from lib import vlcclient
from lib.get_platform import get_platform

DOCKER_MODE = os.environ.get("DOCKER_MODE", "").lower() in ("1", "true", "yes")

if not DOCKER_MODE:
    import pygame
    import qrcode
    if get_platform() != "windows":
        from signal import SIGALRM, alarm, signal

class Karaoke:

    queue = []
    available_songs = []
    now_playing = None
    now_playing_filename = None
    now_playing_user = None
    now_playing_transpose = 0
    is_paused = True
    process = None
    qr_code_path = None
    base_path = os.path.dirname(__file__)
    loop_interval = 500  # in milliseconds
    default_logo_path = os.path.join(base_path, "myvikaraoke.png")
    _cached_logo = None
    _cached_qr = None
    _last_queue_snapshot = None
    _toasts = []

    def __init__(
        self,
        port=5000,
        download_path="/usr/lib/pikaraoke/songs",
        hide_ip=False,
        hide_splash_screen=False,
        high_quality=False,
        log_level=logging.DEBUG,
        splash_delay=2,
        youtubedl_path="youtube-dl",
        vlc_path=None,
        vlc_port=None,
        logo_path=None,
        show_overlay=False
    ):

        self.port = port
        self.hide_ip = hide_ip
        self.hide_splash_screen = hide_splash_screen
        self.download_path = download_path
        self.high_quality = high_quality
        self.splash_delay = int(splash_delay)
        self.youtubedl_path = youtubedl_path
        self.vlc_path = vlc_path
        self.vlc_port = vlc_port
        self.logo_path = self.default_logo_path if logo_path == None else logo_path
        self.show_overlay = show_overlay

        # other initializations
        self.platform = get_platform()
        self.vlcclient = None
        self.screen = None

        logging.basicConfig(
            format="[%(asctime)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            level=int(log_level),
        )

        logging.debug(
            """
    http port: %s
    hide IP: %s
    hide splash: %s
    splash_delay: %s
    high quality video: %s
    download path: %s
    youtube-dl path: %s
    logo path: %s
    VLC path: %s
    VLC port: %s
    log_level: %s
    show overlay: %s"""
            % (
                self.port,
                self.hide_ip,
                self.hide_splash_screen,
                self.splash_delay,
                self.high_quality,
                self.download_path,
                self.youtubedl_path,
                self.logo_path,
                self.vlc_path,
                self.vlc_port,
                log_level,
                self.show_overlay
            )
        )

        # Generate connection URL and QR code, retry in case pi is still starting up
        # and doesn't have an IP yet (occurs when launched from /etc/rc.local)
        end_time = int(time.time()) + 30

        if self.platform == "raspberry_pi":
            while int(time.time()) < end_time:
                addresses_str = check_output(["hostname", "-I"]).strip().decode("utf-8")
                addresses = addresses_str.split(" ")
                self.ip = addresses[0]
                if not self.is_network_connected():
                    logging.debug("Couldn't get IP, retrying....")
                else:
                    break
        else:
            self.ip = self.get_ip()

        logging.debug("IP address (for QR code and splash screen): " + self.ip)

        self.url = "http://%s:%s" % (self.ip, self.port)

        self.set_language('pt_BR')

        # get songs from database (falls back to filesystem scan)
        self.get_available_songs()

        self.get_youtubedl_version()

        # clean up old sessions — kill any leftover VLC processes system-wide
        if not DOCKER_MODE:
            subprocess.call(["pkill", "-f", "vlc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            self.kill_player()

        if DOCKER_MODE:
            from lib.vlc_remote_client import VLCRemoteClient
            self.vlcclient = VLCRemoteClient()
            self.hide_splash_screen = True
            logging.info("Docker mode: using remote VLC client, splash screen disabled")
        else:
            self.generate_qr_code()
            if self.show_overlay:
                self.vlcclient = vlcclient.VLCClient(port=self.vlc_port, path=self.vlc_path, qrcode=self.qr_code_path, url=None if self.hide_ip else self.url)
            else:
                self.vlcclient = vlcclient.VLCClient(port=self.vlc_port, path=self.vlc_path)

        if not self.hide_splash_screen:
            self.initialize_screen()
            self.render_splash_screen()

    def set_language(self, lang):
        lang_translations = gettext.translation('messages', localedir='translations', languages=[lang])
        lang_translations.install()
        self._ = lang_translations.gettext

    # Other ip-getting methods are unreliable and sometimes return 127.0.0.1
    # https://stackoverflow.com/a/28950776
    def get_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # doesn't even have to be reachable
            s.connect(("10.255.255.255", 1))
            IP = s.getsockname()[0]
        except Exception:
            IP = "127.0.0.1"
        finally:
            s.close()
        return IP

    def get_youtubedl_version(self):
        if DOCKER_MODE:
            from lib.download_client import get_version
            self.ytdlp_version = get_version()
            return self.ytdlp_version
        self.ytdlp_version = (
            check_output([self.youtubedl_path, "--version"]).strip().decode("utf8")
        )
        return self.ytdlp_version

    def upgrade_ytdlp(self):
        if DOCKER_MODE:
            from lib.download_client import upgrade
            logging.info("Upgrading yt_dlp via download service...")
            result = upgrade()
            if result:
                self.ytdlp_version = result.get("version", "unknown")
                logging.info("Done. New version: %s" % self.ytdlp_version)
            return
        logging.info(
            "Upgrading yt_dlp, current version: %s" % self.ytdlp_version
        )
        output = check_output([self.youtubedl_path, "-U"]).decode("utf8").strip()
        logging.info(output)
        if "It looks like you installed youtube-dl with a package manager" in output:
            try:
                logging.info("Attempting youtube-dl upgrade via pip3...")
                output = check_output(
                    ["pip3", "install", "--upgrade", "yt_dlp"]
                ).decode("utf8")
            except FileNotFoundError:
                logging.info("Attempting youtube-dl upgrade via pip...")
                output = check_output(
                    ["pip", "install", "--upgrade", "yt_dlp"]
                ).decode("utf8")
            logging.info(output)
        self.get_youtubedl_version()
        logging.info("Done. New version: %s" % self.ytdlp_version)

    def is_network_connected(self):
        return not len(self.ip) < 7

    def generate_qr_code(self):
        if DOCKER_MODE:
            self.qr_code_path = None
            return
        logging.debug("Generating URL QR code")
        qr = qrcode.QRCode(
            version=1,
            box_size=1,
            border=4,
        )
        ws_url = "http://myvikaraoke.local"
        qr.add_data(ws_url)
        qr.make()
        img = qr.make_image()
        self.qr_code_path = os.path.join(self.base_path, "qrcode.png")
        img.save(self.qr_code_path)

    def get_default_display_mode(self):
        if self.platform in ("raspberry_pi", "linux"):
            # NOFRAME + explicit position avoids fullscreen issues on Pi (both 32 and 64-bit)
            os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
            return pygame.NOFRAME
        else:
            return pygame.FULLSCREEN

    def initialize_screen(self):
        if not self.hide_splash_screen:
            logging.debug("Initializing pygame")
            self.full_screen = True
            pygame.display.init()
            pygame.display.set_caption("pikaraoke")
            pygame.font.init()
            pygame.mouse.set_visible(0)
            self.font = pygame.font.SysFont(pygame.font.get_default_font(), 40)
            # Read desktop resolution after display init, before set_mode
            info = pygame.display.Info()
            self.width = info.current_w
            self.height = info.current_h
            logging.debug("Initializing screen mode (%dx%d)" % (self.width, self.height))

            if self.platform == "windows":
                self.screen = pygame.display.set_mode(
                    [self.width, self.height], self.get_default_display_mode()
                )
            else:
                # this section is an unbelievable nasty hack - for some reason Pygame
                # needs a keyboardinterrupt to initialise in some limited circumstances
                # source: https://stackoverflow.com/questions/17035699/pygame-requires-keyboard-interrupt-to-init-display
                class Alarm(Exception):
                    pass

                def alarm_handler(signum, frame):
                    raise Alarm

                signal(SIGALRM, alarm_handler)
                alarm(3)
                try:
                    self.screen = pygame.display.set_mode(
                        [self.width, self.height], self.get_default_display_mode()
                    )
                    # Tell the WM to raise this window above the taskbar (desktop fullscreen)
                    if self.platform in ("raspberry_pi", "linux"):
                        pygame.display.toggle_fullscreen()
                    alarm(0)
                except Alarm:
                    raise KeyboardInterrupt
            logging.debug("Done initializing splash screen")

    def toggle_full_screen(self):
        if not self.hide_splash_screen:
            logging.debug("Toggling fullscreen...")
            if self.full_screen:
                self.screen = pygame.display.set_mode([1280, 720])
                self.render_splash_screen()
                self.full_screen = False
            else:
                self.screen = pygame.display.set_mode(
                    [self.width, self.height], self.get_default_display_mode()
                )
                self.render_splash_screen()
                self.full_screen = True

    def render_splash_screen(self):
        if not self.hide_splash_screen:
            # Pygame display ops are not thread-safe; skip when called from Flask threads
            if threading.current_thread() is not threading.main_thread():
                return
            logging.debug("Rendering splash screen")

            self.screen.fill((0, 0, 0))

            # Cache images to avoid disk reads every frame
            if self._cached_logo is None:
                self._cached_logo = pygame.image.load(self.logo_path)
            if self._cached_qr is None:
                self._cached_qr = pygame.transform.scale(
                    pygame.image.load(self.qr_code_path), (150, 150)
                )

            logo = self._cached_logo
            logo_rect = logo.get_rect(center=self.screen.get_rect().center)
            self.screen.blit(logo, logo_rect)

            blitY = self.screen.get_rect().bottomleft[1] - 40

            p_image = self._cached_qr
            self.screen.blit(p_image, (20, blitY - 125))
            if not self.is_network_connected():
                text = self.font.render(
                    "Wifi/Network not connected. Shutting down in 10s...",
                    True,
                    (255, 255, 255),
                )
                self.screen.blit(text, (p_image.get_width() + 35, blitY))
                time.sleep(10)
                logging.info(
                    "No IP found. Network/Wifi configuration required. For wifi config, try: sudo raspi-config or the desktop GUI: startx"
                )
                self.stop()
            else:
                if not self.hide_ip:
                    text = self.font.render(
                        "Connect at: http://myvikaraoke.local", True, (255, 255, 255)
                    )
                else:
                    text = self.font.render("", True, (255, 255, 255))
                self.screen.blit(text, (p_image.get_width() + 35, blitY))

            self.render_queue_overlay()
            pygame.display.update()

    def _render_song_item(self, title, user, x, y, color=(220, 220, 220),
                          prefix="", font_size=24, show_user=True):
        """Shared helper to render a song item on the pygame screen.

        Used by both render_queue_overlay() and render_next_song_to_splash_screen()
        to keep the display format consistent and maintained in one place.
        Returns the y position after the rendered item.
        """
        font_item = pygame.font.SysFont(pygame.font.get_default_font(), font_size)
        font_small = pygame.font.SysFont(pygame.font.get_default_font(), max(font_size - 4, 14))
        max_title_len = 38 if font_size <= 24 else 50

        display_title = title
        if len(display_title) > max_title_len:
            display_title = display_title[:max_title_len] + "..."
        label = prefix + display_title if prefix else display_title
        title_surface = font_item.render(label, True, color)
        self.screen.blit(title_surface, (x, y))
        y += font_item.get_linesize()
        if show_user and user:
            user_surface = font_small.render("   " + user, True, (140, 140, 140))
            self.screen.blit(user_surface, (x, y))
            y += font_small.get_linesize() + 4
        return y

    def render_next_song_to_splash_screen(self):
        if not self.hide_splash_screen:
            self.render_splash_screen()
            if len(self.queue) >= 1:
                logging.debug("Rendering next song to splash screen")
                next_song = self.queue[0]["title"]
                next_user = self.queue[0]["user"]

                font_header = pygame.font.SysFont(pygame.font.get_default_font(), 44)
                up_next_txt = self._("Up next")

                # Render header + song using shared helper, right-aligned
                header = font_header.render(up_next_txt, True, COLORS['blue'])
                header_x = self.width - header.get_width() - 20
                self.screen.blit(header, (header_x, 8))

                # Song title and user below the header, right-aligned
                font_song = pygame.font.SysFont(pygame.font.get_default_font(), 36)
                max_len = 50
                display_title = next_song if len(next_song) <= max_len else next_song[:max_len] + "..."
                title_surface = font_song.render(display_title, True, COLORS['green'])
                title_x = self.width - title_surface.get_width() - 20
                self.screen.blit(title_surface, (title_x, 55))

                font_user = pygame.font.SysFont(pygame.font.get_default_font(), 28)
                user_surface = font_user.render(next_user, True, COLORS['white'])
                user_x = self.width - user_surface.get_width() - 20
                self.screen.blit(user_surface, (user_x, 95))

                pygame.display.update()
                return True
            else:
                logging.debug("Could not render next song to splash. No song in queue")
                return False

    def get_search_results(self, textToSearch):
        logging.info("Searching YouTube for: " + textToSearch)
        num_results = 10
        yt_search = 'ytsearch%d:"%s"' % (num_results, unidecode(textToSearch))
        cmd = [self.youtubedl_path, "-j", "--no-playlist", "--flat-playlist", yt_search]
        logging.debug("Youtube-dl search command: " + " ".join(cmd))
        try:
            output = subprocess.check_output(cmd).decode("utf-8")
            logging.debug("Search results: " + output)
            rc = []
            video_url_base = ""#"https://www.youtube.com/watch?v="
            for each in output.split("\n"):
                if len(each) > 2:
                    j = json.loads(each)
                    if (not "title" in j) or (not "url" in j):
                        continue
                    rc.append([j["title"], video_url_base + j["url"], j["id"]])
            return rc
        except Exception as e:
            logging.debug("Error while executing search: " + str(e))
            raise e

    def get_karaoke_search_results(self, songTitle):
        return self.get_search_results(songTitle + " karaoke")

    def download_video(self, video_url, enqueue=False, user="Pikaraoke"):
        logging.info("Downloading video: " + video_url)
        dl_path = self.download_path + "%(title)s---%(id)s.%(ext)s"
        file_quality = (
            "bestvideo[ext!=webm][height<=1080]+bestaudio[ext!=webm]/best[ext!=webm]"
            if self.high_quality
            else "mp4"
        )
        cmd = [self.youtubedl_path, "-f", file_quality, "--write-sub", "-o", dl_path, video_url]
        logging.debug("Youtube-dl command: " + " ".join(cmd))
        rc = subprocess.call(cmd)
        if rc != 0:
            logging.error("Error code while downloading, retrying once...")
            rc = subprocess.call(cmd)  # retry once. Seems like this can be flaky
        if rc == 0:
            logging.debug("Song successfully downloaded: " + video_url)
            self.get_available_songs()
            if enqueue:
                y = self.get_youtube_id_from_url(video_url)
                s = self.find_song_by_youtube_id(y)
                if s:
                    self.enqueue(s, user)
                else:
                    logging.error("Error queueing song: " + video_url)
        else:
            logging.error("Error downloading song: " + video_url)
        return rc

    def get_available_songs(self):
        db_path = os.path.join(self.download_path, 'karaoke.db')
        if not os.path.exists(db_path):
            logging.warning("karaoke.db not found at %s, falling back to filesystem scan" % db_path)
            self._get_available_songs_from_fs()
            return
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT filepath FROM songs ORDER BY lower(clean_filename)"
            ).fetchall()
            conn.close()
            self.available_songs = [r["filepath"] for r in rows]
            logging.info(f"Loaded {len(self.available_songs)} songs from karaoke.db")
        except Exception as e:
            logging.error(f"get_available_songs DB error: {e}")
            self._get_available_songs_from_fs()

    def _get_available_songs_from_fs(self):
        logging.info("Fetching available songs from filesystem: " + self.download_path)
        types = ['.mp4', '.mp3', '.zip', '.mkv', '.avi', '.webm', '.mov']
        files_grabbed = []
        P = Path(self.download_path)
        for file in P.rglob('*.*'):
            base, ext = os.path.splitext(file.as_posix())
            if ext.lower() in types:
                if os.path.isfile(file.as_posix()):
                    files_grabbed.append(file.as_posix())
        self.available_songs = sorted(files_grabbed, key=lambda f: str.lower(os.path.basename(f)))
        logging.info(f"Loaded {len(self.available_songs)} songs from filesystem")

    def delete(self, song_path):
        logging.info("Deleting song: " + song_path)
        os.remove(song_path)
        ext = os.path.splitext(song_path)
        # if we have an associated cdg file, delete that too
        cdg_file = song_path.replace(ext[1],".cdg")
        if (os.path.exists(cdg_file)):
            os.remove(cdg_file)
        
        self.get_available_songs()

    def rename(self, song_path, new_name):
        logging.info("Renaming song: '" + song_path + "' to: " + new_name)
        ext = os.path.splitext(song_path)
        if len(ext) == 2:
            new_file_name = new_name + ext[1]
        os.rename(song_path, self.download_path + new_file_name)
        # if we have an associated cdg file, rename that too
        cdg_file = song_path.replace(ext[1],".cdg")
        if (os.path.exists(cdg_file)):
            os.rename(cdg_file, self.download_path + new_name + ".cdg")
        self.get_available_songs()

    def filename_from_path(self, file_path):
        rc = os.path.basename(file_path)
        rc = os.path.splitext(rc)[0]
        rc = rc.split("---")[0]  # removes youtube id if present
        return rc

    def find_song_by_youtube_id(self, youtube_id):
        for each in self.available_songs:
            if youtube_id in each:
                return each
        logging.error("No available song found with youtube id: " + youtube_id)
        return None

    def get_youtube_id_from_url(self, url):
        s = url.split("watch?v=")
        if len(s) == 2:
            return s[1]
        else:
            logging.error("Error parsing youtube id from url: " + url)
            return None

    def kill_player(self):
        logging.debug("Killing old VLC processes")
        if self.vlcclient != None:
            self.vlcclient.kill()

    def play_file(self, file_path, semitones=0, seek_to=None):
        self.now_playing = self.filename_from_path(file_path)
        self.now_playing_filename = file_path

        logging.info("Playing video in VLC: " + self.now_playing)
        if semitones == 0:
            self.vlcclient.play_file(file_path)
        else:
            self.vlcclient.play_file_transpose(file_path, semitones, seek_to=seek_to)

        self.is_paused = False
        self.render_splash_screen()  # remove old previous track

    def play_transposed_file(self, transposed_path, original_path, semitones, seek_to=0.0):
        """Play a pre-processed transposed file at a given position.

        Used when a cached transposed version already exists on disk,
        so no ffmpeg processing is needed — just swap the VLC playback.
        Handles .zip CDG files by extracting them first.
        """
        logging.info(
            "Playing cached transpose (%+d) at %.1fs: %s"
            % (semitones, seek_to, transposed_path)
        )
        self.now_playing = self.filename_from_path(original_path)
        self.now_playing_filename = original_path
        self.now_playing_transpose = semitones

        # Prevent main loop from detecting brief VLC downtime as "song ended"
        self.vlcclient.is_transposing = True

        self.vlcclient.kill()
        if self.vlcclient.process:
            try:
                self.vlcclient.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        time.sleep(0.3)

        # process_file handles .zip extraction, .mp3+.cdg pairing, etc.
        resolved_path = self.vlcclient.process_file(transposed_path)

        command = self.vlcclient.cmd_base + [
            "--start-time", "%.1f" % seek_to, resolved_path
        ]
        self.vlcclient.process = subprocess.Popen(
            command,
            shell=(self.vlcclient.platform == "windows"),
            stdin=subprocess.PIPE,
        )
        self.is_paused = False

        # Clear transposing flag after new VLC process is stable
        timer = threading.Timer(2.0, self.vlcclient.set_transposing_complete)
        timer.daemon = True
        timer.start()

    def transpose_current(self, semitones, from_beginning=False):
        semitones = int(semitones)
        logging.info("Transposing song by %s semitones (from_beginning=%s)" % (semitones, from_beginning))
        self.now_playing_transpose = semitones

        # Capture playback position and duration before VLC is stopped
        if from_beginning:
            seek_to = 0.0
        else:
            seek_to = self.vlcclient._get_current_time() if self.vlcclient.is_running() else 0.0

        _, duration = self.get_playback_position()
        self.vlcclient.transpose_duration = duration if duration > 0 else 1.0

        threading.Thread(
            target=self.play_file,
            args=(self.now_playing_filename, semitones),
            kwargs={"seek_to": seek_to},
            daemon=True
        ).start()

    def is_file_playing(self):
        return self.vlcclient is not None and self.vlcclient.is_running()

    def is_song_in_queue(self, song_path):
        for each in self.queue:
            if each["file"] == song_path:
                return True
        return False

    def enqueue(self, song_path, user="Pikaraoke"):
        if (self.is_song_in_queue(song_path)):
            logging.warning("Song is already in queue, will not add: " + song_path)
            return False
        else:
            logging.info("'%s' is adding song to queue: %s" % (user, song_path))
            title = self.filename_from_path(song_path)
            self.queue.append({"user": user, "file": song_path, "title": title})
            self.show_toast("%s: %s" % (user, title[:40]))
            return True

    def queue_add_random(self, amount):
        logging.info("Adding %d random songs to queue" % amount)
        songs = list(self.available_songs)  # make a copy
        if len(songs) == 0:
            logging.warning("No available songs!")
            return False
        i = 0
        while i < amount:
            r = random.randint(0, len(songs) - 1)
            if self.is_song_in_queue(songs[r]):
                logging.warning("Song already in queue, trying another... " + songs[r])
            else:
                self.queue.append({"user": "Randomizer", "file": songs[r], "title": self.filename_from_path(songs[r])})
                i += 1
            songs.pop(r)
            if len(songs) == 0:
                logging.warning("Ran out of songs!")
                return False
        return True

    def queue_clear(self):
        logging.info("Clearing queue!")
        self.queue = []
        self.skip()

    def queue_edit(self, song_name, action):
        index = 0
        song = None
        for each in self.queue:
            if song_name in each["file"]:
                song = each
                break
            else:
                index += 1
        if song == None:
            logging.error("Song not found in queue: " + song["file"])
            return False
        if action == "up":
            if index < 1:
                logging.warning("Song is up next, can't bump up in queue: " + song["file"])
                return False
            else:
                logging.info("Bumping song up in queue: " + song["file"])
                del self.queue[index]
                self.queue.insert(index - 1, song)
                return True
        elif action == "down":
            if index == len(self.queue) - 1:
                logging.warning(
                    "Song is already last, can't bump down in queue: " + song["file"]
                )
                return False
            else:
                logging.info("Bumping song down in queue: " + song["file"])
                del self.queue[index]
                self.queue.insert(index + 1, song)
                return True
        elif action == "delete":
            logging.info("Deleting song from queue: " + song["file"])
            del self.queue[index]
            return True
        else:
            logging.error("Unrecognized direction: " + action)
            return False

    def skip(self):
        if self.is_file_playing():
            logging.info("Skipping: " + self.now_playing)
            self.vlcclient.stop()
            self.reset_now_playing()
            return True
        else:
            logging.warning("Tried to skip, but no file is playing!")
            return False

    def pause(self):
        if self.is_file_playing():
            logging.info("Toggling pause: " + self.now_playing)
            if self.vlcclient.is_playing():
                self.vlcclient.pause()
            else:
                self.vlcclient.play()
            self.is_paused = not self.is_paused
            return True
        else:
            logging.warning("Tried to pause, but no file is playing!")
            return False

    def vol_up(self):
        if self.is_file_playing():
            self.vlcclient.vol_up()
            return True
        else:
            logging.warning("Tried to volume up, but no file is playing!")
            return False

    def vol_down(self):
        if self.is_file_playing():
            self.vlcclient.vol_down()
            return True
        else:
            logging.warning("Tried to volume down, but no file is playing!")
            return False

    def restart(self):
        if self.is_file_playing():
            self.vlcclient.restart()
            self.is_paused = False
            return True
        else:
            logging.warning("Tried to restart, but no file is playing!")
            return False

    def seek(self, seconds):
        if self.is_file_playing():
            self.vlcclient.seek(seconds)
            return True
        return False

    def get_playback_position(self):
        """Return (position, duration) in seconds, or (0, 0)."""
        if self.is_file_playing():
            try:
                return self.vlcclient.get_time_and_length()
            except Exception:
                pass
        return 0.0, 0.0

    def stop(self):
        self.running = False

    def render_queue_overlay(self):
        """Render the song queue on the left side of the splash screen.

        Uses _render_song_item() for consistent formatting with the next-song screen.
        """
        if self.hide_splash_screen:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        font_title = pygame.font.SysFont(pygame.font.get_default_font(), 30)
        font_small = pygame.font.SysFont(pygame.font.get_default_font(), 20)

        x = 20
        y = 15
        max_y = self.height - 200  # reserve space for QR code

        # Header
        header = font_title.render("Fila", True, (255, 255, 255))
        self.screen.blit(header, (x, y))
        y += 40

        # Now playing
        if self.now_playing:
            y = self._render_song_item(
                self.now_playing, self.now_playing_user, x, y,
                color=(34, 154, 91), prefix=u"\u25B6 ", show_user=False,
            )
            # Time + user on the same detail line
            pos, dur = self.get_playback_position()
            if dur > 0:
                time_str = "%d:%02d / %d:%02d" % (
                    pos // 60, pos % 60, dur // 60, dur % 60
                )
            else:
                time_str = ""
            user_str = self.now_playing_user or ""
            info_text = font_small.render(
                "   " + time_str + "  " + user_str, True, (180, 180, 180)
            )
            self.screen.blit(info_text, (x, y))
            y += font_small.get_linesize() + 6

        # Queued songs
        for i, item in enumerate(self.queue):
            if y > max_y:
                remaining = len(self.queue) - i
                more = font_small.render(
                    "   + %d mais..." % remaining, True, (120, 120, 120)
                )
                self.screen.blit(more, (x, y))
                break
            y = self._render_song_item(
                item["title"], item["user"], x, y,
                prefix="%d. " % (i + 1),
            )

    def show_toast(self, text, duration=5):
        """Queue a toast notification to appear on the TV screen."""
        self._toasts.append({
            "text": text,
            "expires": time.time() + duration,
        })
        # Also show via VLC marquee if a song is playing
        if self.vlcclient and self.is_file_playing():
            try:
                self.vlcclient.command(
                    "marquee&val=%s" % text.replace(" ", "%20").replace("&", "%26")
                )
                # Auto-hide after duration (VLC marquee timeout in ms)
                self.vlcclient.command("marquee-timeout&val=%d" % (duration * 1000))
            except Exception as e:
                logging.debug("VLC marquee failed: %s" % e)

    def _render_toasts(self):
        """Render active toast notifications on the pygame screen."""
        if self.hide_splash_screen:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        now = time.time()
        # Remove expired toasts
        self._toasts = [t for t in self._toasts if t["expires"] > now]
        if not self._toasts:
            return
        font_toast = pygame.font.SysFont(pygame.font.get_default_font(), 28)
        y = 10
        for toast in self._toasts:
            # Semi-transparent background
            text_surface = font_toast.render(toast["text"], True, (255, 255, 255))
            tw = text_surface.get_width() + 20
            th = text_surface.get_height() + 12
            tx = self.width - tw - 10
            bg = pygame.Surface((tw, th), pygame.SRCALPHA)
            bg.fill((0, 0, 0, 180))
            self.screen.blit(bg, (tx, y))
            self.screen.blit(text_surface, (tx + 10, y + 6))
            y += th + 5

    def render_transpose_overlay(self):
        """Render a 'Changing key...' overlay with progress bar on the TV."""
        if self.hide_splash_screen:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        progress = self.vlcclient.transpose_progress  # 0-100
        semitones = self.now_playing_transpose
        song = self.now_playing or ""

        self.screen.fill((0, 0, 0))

        # Song title
        title = self.font.render(song, True, (255, 255, 255))
        title_rect = title.get_rect(centerx=self.width // 2, y=self.height // 3 - 60)
        self.screen.blit(title, title_rect)

        # Subtitle: "Changing key: +3 semitones"
        sub_font = pygame.font.SysFont(pygame.font.get_default_font(), 36)
        sign = "+" if semitones > 0 else ""
        sub_text = self._("Changing key") + ": %s%d " % (sign, semitones) + self._("semitones")
        sub = sub_font.render(sub_text, True, (255, 200, 80))
        sub_rect = sub.get_rect(centerx=self.width // 2, y=self.height // 3)
        self.screen.blit(sub, sub_rect)

        # Progress bar
        bar_width = self.width // 2
        bar_height = 40
        bar_x = (self.width - bar_width) // 2
        bar_y = self.height // 2
        # Gray background
        pygame.draw.rect(self.screen, (80, 80, 80), (bar_x, bar_y, bar_width, bar_height))
        # Green fill
        fill_w = int(bar_width * progress / 100)
        if fill_w > 0:
            pygame.draw.rect(self.screen, (34, 154, 91), (bar_x, bar_y, fill_w, bar_height))
        # White border
        pygame.draw.rect(self.screen, (255, 255, 255), (bar_x, bar_y, bar_width, bar_height), 2)

        # Percentage text
        pct_text = self.font.render("%d%%" % progress, True, (255, 255, 255))
        pct_rect = pct_text.get_rect(centerx=self.width // 2, y=bar_y + bar_height + 15)
        self.screen.blit(pct_text, pct_rect)

        pygame.display.update()

    def handle_run_loop(self):
        if self.hide_splash_screen:
            time.sleep(self.loop_interval / 1000)
        else:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    logging.warning("Window closed: Exiting pikaraoke...")
                    self.running = False
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        logging.warning("ESC pressed: Exiting pikaraoke...")
                        self.running = False
                    if event.key == pygame.K_f:
                        self.toggle_full_screen()

            # Show transpose progress overlay on TV when transposing
            if self.vlcclient and self.vlcclient.is_transposing:
                self.render_transpose_overlay()
            else:
                # Re-render splash when queue or now_playing changes
                queue_snapshot = (
                    self.now_playing,
                    self.now_playing_user,
                    [(q["file"], q["user"]) for q in self.queue],
                )
                if queue_snapshot != self._last_queue_snapshot:
                    self._last_queue_snapshot = queue_snapshot
                    if not self.is_file_playing():
                        self.render_splash_screen()
                # Render toast notifications over the current screen
                self._render_toasts()
                pygame.display.update()

            pygame.time.wait(self.loop_interval)

    # Use this to reset the screen in case it loses focus
    # This seems to occur in windows after playing a video
    def pygame_reset_screen(self):
        if self.hide_splash_screen:
            pass
        else:
            logging.debug("Resetting pygame screen...")
            pygame.display.quit()
            self._cached_logo = None
            self._cached_qr = None
            self.initialize_screen()
            self.render_splash_screen()

    def reset_now_playing(self):
        self.now_playing = None
        self.now_playing_filename = None
        self.now_playing_user = None
        self.is_paused = True
        self.now_playing_transpose = 0
        # Ensure the transpose overlay doesn't stay stuck on screen
        if self.vlcclient:
            self.vlcclient.is_transposing = False
            self.vlcclient.transpose_progress = 0

    def run(self):
        logging.info("Starting PiKaraoke!")
        self.running = True
        while self.running:
            try:
                if not self.is_file_playing() and self.now_playing != None:
                    self.reset_now_playing()
                if len(self.queue) > 0:
                    if not self.is_file_playing():
                        self.reset_now_playing()
                        if not self.hide_splash_screen and not pygame.display.get_active():
                            self.pygame_reset_screen()
                        self.render_next_song_to_splash_screen()
                        i = 0
                        while i < (self.splash_delay * 1000):
                            self.handle_run_loop()
                            i += self.loop_interval
                        self.play_file(self.queue[0]["file"])
                        self.now_playing_user=self.queue[0]["user"]
                        self.queue.pop(0)
                elif not self.hide_splash_screen and not pygame.display.get_active() and not self.is_file_playing():
                    self.pygame_reset_screen()
                self.handle_run_loop()
            except KeyboardInterrupt:
                logging.warning("Keyboard interrupt: Exiting pikaraoke...")
                self.running = False

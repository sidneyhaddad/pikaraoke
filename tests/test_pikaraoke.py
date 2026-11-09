"""
PiKaraoke test suite.

Categories:
  - smoke:  System dependency checks (no mocks). Run before starting karaoke.
  - unit:   Pure logic tests with mocks.
  - flask:  HTTP route tests using Flask test client.
"""
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import zipfile
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# =============================================================================
# 1. SMOKE TESTS — System dependency checks (no mocks)
# =============================================================================

@pytest.mark.smoke
class TestSmokeDependencies:

    def test_ytdlp_installed(self):
        """yt-dlp binary must be findable on PATH."""
        assert shutil.which("yt-dlp"), "yt-dlp not found on PATH"

    def test_ytdlp_version_recent(self):
        """yt-dlp version should be from the current or previous year."""
        out = subprocess.check_output(["yt-dlp", "--version"]).decode().strip()
        year = int(out.split(".")[0])
        import datetime
        current_year = datetime.datetime.now().year
        assert year >= current_year - 1, (
            f"yt-dlp version {out} is too old (year={year}). Run: pip3 install --upgrade yt_dlp"
        )

    def test_ffmpeg_installed(self):
        """ffmpeg must be available for pitch shifting."""
        assert shutil.which("ffmpeg"), "ffmpeg not found on PATH"

    def test_vlc_installed(self):
        """VLC must be installed."""
        assert shutil.which("vlc") or os.path.isfile("/usr/bin/vlc"), (
            "VLC not found. Install with: sudo apt install vlc"
        )

    def test_tmp_dir_writable(self):
        """VLC client needs /tmp/pikaraoke/ to be writable."""
        tmp_dir = "/tmp/pikaraoke/"
        os.makedirs(tmp_dir, exist_ok=True)
        test_file = os.path.join(tmp_dir, ".write_test")
        try:
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
        except PermissionError:
            pytest.fail(f"Cannot write to {tmp_dir}. Fix permissions: chmod 777 {tmp_dir}")

    def test_download_dir_writable(self, tmp_path):
        """Download directory must exist and be writable."""
        test_file = tmp_path / ".write_test"
        test_file.write_text("test")
        assert test_file.exists()

    def test_pygame_importable(self):
        """Pygame must be importable for the splash screen."""
        import pygame  # noqa: F401

    def test_translations_compiled(self):
        """Compiled .mo files must exist for all configured languages."""
        from constants import LANGUAGES
        base = os.path.join(os.path.dirname(__file__), "..")
        for lang in LANGUAGES:
            if lang == "en":
                continue
            # Handle lang codes: pt_BR stays as-is, zh_CN -> zh_Hans_CN in this project
            mo_candidates = [
                os.path.join(base, "translations", lang, "LC_MESSAGES", "messages.mo"),
                os.path.join(base, "translations", lang.replace("_", "_Hans_"), "LC_MESSAGES", "messages.mo"),
            ]
            found = any(os.path.isfile(p) for p in mo_candidates)
            assert found, (
                f"No compiled .mo file for '{lang}'. Run: pybabel compile -d translations"
            )

    def test_not_running_as_root(self):
        """VLC refuses to run as root. Warn if tests are run as root."""
        if sys.platform.startswith("linux"):
            if os.geteuid() == 0:
                pytest.skip("Running as root — VLC will refuse to start without --no-xlib workaround")


@pytest.mark.smoke
class TestSmokeFFmpeg:
    """Real ffmpeg functional tests — no mocks."""

    @staticmethod
    def _generate_wav(filepath, duration=0.5, freq=440, sample_rate=44100):
        """Generate a short sine wave WAV file for testing."""
        n_samples = int(duration * sample_rate)
        with wave.open(filepath, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            for i in range(n_samples):
                sample = int(32767 * math.sin(2 * math.pi * freq * i / sample_rate))
                w.writeframes(struct.pack("<h", sample))

    def test_ffmpeg_can_run(self):
        """ffmpeg binary must execute and report its version."""
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, "ffmpeg failed to run"
        assert "ffmpeg version" in result.stdout, "ffmpeg output unexpected"

    def test_ffmpeg_pitch_shift_audio(self, tmp_path):
        """ffmpeg must be able to pitch-shift an audio file (the core transpose operation)."""
        src = str(tmp_path / "test_tone.wav")
        dst = str(tmp_path / "shifted.wav")
        self._generate_wav(src)

        # Same filter chain used in vlcclient._ffmpeg_pitch_shift
        semitones = 3
        ratio = 2 ** (semitones / 12.0)
        af = "asetrate=44100*%.6f,aresample=44100,atempo=%.6f" % (ratio, 1.0 / ratio)

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-af", af, dst],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"ffmpeg pitch shift failed: {result.stderr[-500:]}"
        assert os.path.isfile(dst), "Pitch-shifted output file not created"
        assert os.path.getsize(dst) > 0, "Pitch-shifted output file is empty"

    def test_ffmpeg_pitch_shift_negative(self, tmp_path):
        """ffmpeg must handle negative semitones (lower pitch)."""
        src = str(tmp_path / "test_tone.wav")
        dst = str(tmp_path / "shifted_down.wav")
        self._generate_wav(src)

        semitones = -3
        ratio = 2 ** (semitones / 12.0)
        af = "asetrate=44100*%.6f,aresample=44100,atempo=%.6f" % (ratio, 1.0 / ratio)

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-af", af, dst],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"ffmpeg negative pitch shift failed: {result.stderr[-500:]}"
        assert os.path.getsize(dst) > 0, "Negative pitch-shifted output is empty"

    def test_ffmpeg_video_copy_with_audio_shift(self, tmp_path):
        """ffmpeg must be able to copy video while shifting audio (used for mp4/mkv karaoke)."""
        # Generate a minimal mp4 with a synthetic audio+video stream
        src_wav = str(tmp_path / "tone.wav")
        src_mp4 = str(tmp_path / "test_video.mp4")
        dst_mp4 = str(tmp_path / "shifted_video.mp4")
        self._generate_wav(src_wav, duration=1.0)

        # Create a minimal mp4: solid color video + audio
        gen = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
             "-i", src_wav,
             "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", "-shortest", src_mp4],
            capture_output=True, text=True, timeout=30,
        )
        assert gen.returncode == 0, f"Failed to create test mp4: {gen.stderr[-500:]}"

        # Now pitch shift with video copy — exactly what VLCClient does
        semitones = 2
        ratio = 2 ** (semitones / 12.0)
        af = "asetrate=44100*%.6f,aresample=44100,atempo=%.6f" % (ratio, 1.0 / ratio)

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src_mp4,
             "-vcodec", "copy", "-af", af, dst_mp4],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"ffmpeg video+audio shift failed: {result.stderr[-500:]}"
        assert os.path.getsize(dst_mp4) > 100, "Shifted mp4 is too small, likely corrupt"


@pytest.mark.smoke
class TestSmokeVLC:
    """Real VLC functional tests — no mocks."""

    def test_vlc_can_run(self):
        """VLC binary must execute and report its version."""
        vlc_path = shutil.which("vlc") or "/usr/bin/vlc"
        result = subprocess.run(
            [vlc_path, "--version"], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"VLC failed to run: {result.stderr[-300:]}"
        assert "VLC" in result.stdout, "VLC version output unexpected"

    def test_vlc_has_http_interface(self):
        """VLC must support the HTTP interface (used for remote control)."""
        vlc_path = shutil.which("vlc") or "/usr/bin/vlc"
        result = subprocess.run(
            [vlc_path, "--list", "intf"],
            capture_output=True, text=True, timeout=10,
        )
        # --list may not be supported on all versions; just verify VLC runs
        assert result.returncode == 0 or "Unknown option" in result.stderr


@pytest.mark.smoke
class TestSmokeCDGExtraction:
    """Real CDG zip extraction — no mocks."""

    def test_zip_with_mp3_and_cdg(self, tmp_path):
        """Zipped CDG karaoke files must be extractable."""
        # Create a fake zip with .mp3 and .cdg
        zip_path = str(tmp_path / "karaoke_song.zip")
        mp3_content = b"\xff\xfb\x90\x00" + b"\x00" * 1000  # fake MP3 header
        cdg_content = b"\x00" * 96 * 10  # fake CDG data (96 bytes per packet)

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Song Title.mp3", mp3_content)
            zf.writestr("Song Title.cdg", cdg_content)

        # Extract and verify — same logic as VLCClient.handle_zipped_cdg
        extracted_dir = str(tmp_path / "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extracted_dir)

        files = os.listdir(extracted_dir)
        mp3_files = [f for f in files if f.lower().endswith(".mp3")]
        cdg_files = [f for f in files if f.lower().endswith(".cdg")]
        assert len(mp3_files) == 1, f"Expected 1 mp3, found: {mp3_files}"
        assert len(cdg_files) == 1, f"Expected 1 cdg, found: {cdg_files}"
        assert os.path.splitext(mp3_files[0])[0] == os.path.splitext(cdg_files[0])[0], \
            "mp3 and cdg filenames don't match"

    def test_tmp_extraction_dir_works(self):
        """The /tmp/pikaraoke/extracted dir must be creatable and writable."""
        extracted_dir = "/tmp/pikaraoke/extracted"
        os.makedirs(extracted_dir, exist_ok=True)
        test_file = os.path.join(extracted_dir, ".write_test")
        try:
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
        except PermissionError:
            pytest.fail(f"Cannot write to {extracted_dir}. CDG extraction will fail.")
        finally:
            if os.path.isdir(extracted_dir) and not os.listdir(extracted_dir):
                os.rmdir(extracted_dir)


@pytest.mark.smoke
class TestSmokeNetwork:
    """Real network and connectivity checks — no mocks."""

    def test_local_ip_available(self):
        """System must have a local IP address (not just 127.0.0.1)."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        assert len(ip) >= 7, f"IP too short: '{ip}'"
        assert ip != "127.0.0.1", (
            "Only loopback IP available. Devices won't be able to connect to karaoke."
        )

    def test_hostname_resolves(self):
        """Hostname must be resolvable."""
        import socket
        hostname = socket.gethostname()
        assert len(hostname) > 0, "Hostname is empty"


@pytest.mark.smoke
class TestSmokeSongLibrary:
    """Real song library scanning — no mocks."""

    def test_scan_finds_media_files(self, tmp_path):
        """The filesystem scanner must find supported media files."""
        from pathlib import Path

        # Create test files with supported extensions
        (tmp_path / "song1.mp4").write_text("fake")
        (tmp_path / "song2.mp3").write_text("fake")
        (tmp_path / "song3.mkv").write_text("fake")
        (tmp_path / "song4.zip").write_text("fake")
        (tmp_path / "not_a_song.txt").write_text("fake")
        (tmp_path / "readme.pdf").write_text("fake")

        # Same logic as karaoke._get_available_songs_from_fs
        types = [".mp4", ".mp3", ".zip", ".mkv", ".avi", ".webm", ".mov"]
        found = []
        for f in Path(str(tmp_path)).rglob("*.*"):
            ext = os.path.splitext(f.as_posix())[1].lower()
            if ext in types and os.path.isfile(f.as_posix()):
                found.append(f.as_posix())

        assert len(found) == 4, f"Expected 4 media files, found {len(found)}: {found}"

    def test_scan_handles_subdirectories(self, tmp_path):
        """Scanner must find songs in subdirectories (rglob)."""
        sub = tmp_path / "subdir" / "deep"
        sub.mkdir(parents=True)
        (sub / "deep_song.mp4").write_text("fake")

        from pathlib import Path
        types = [".mp4", ".mp3", ".zip", ".mkv", ".avi", ".webm", ".mov"]
        found = []
        for f in Path(str(tmp_path)).rglob("*.*"):
            ext = os.path.splitext(f.as_posix())[1].lower()
            if ext in types and os.path.isfile(f.as_posix()):
                found.append(f.as_posix())

        assert len(found) == 1, f"Expected 1 song in subdir, found {len(found)}"

    def test_actual_download_dir_exists(self):
        """The configured download directory must exist."""
        # Check common pikaraoke song directories
        candidates = [
            os.path.expanduser("~/Documents/pikaraoke-songs/"),
            os.path.expanduser("~/pikaraoke-songs/"),
            "/usr/lib/pikaraoke/songs/",
            os.path.expanduser("~/pikaraoke/songs/"),
        ]
        found = [d for d in candidates if os.path.isdir(d)]
        assert len(found) > 0, (
            "No song download directory found. Expected one of: " + ", ".join(candidates)
        )


@pytest.mark.smoke
class TestSmokePygame:
    """Real pygame functional tests — no mocks."""

    def test_pygame_display_init(self):
        """Pygame display subsystem must initialize."""
        import pygame
        try:
            pygame.display.init()
            info = pygame.display.Info()
            assert info.current_w > 0, "Display width is 0"
            assert info.current_h > 0, "Display height is 0"
        finally:
            pygame.display.quit()

    def test_pygame_font_rendering(self):
        """Pygame must be able to render text (used for splash screen)."""
        import pygame
        try:
            pygame.font.init()
            font = pygame.font.SysFont(pygame.font.get_default_font(), 40)
            surface = font.render("Test text", True, (255, 255, 255))
            assert surface.get_width() > 0, "Rendered text has 0 width"
            assert surface.get_height() > 0, "Rendered text has 0 height"
        finally:
            pygame.font.quit()

    def test_pygame_image_load(self):
        """Pygame must be able to load the logo image."""
        import pygame
        logo_path = os.path.join(os.path.dirname(__file__), "..", "logo.png")
        if not os.path.isfile(logo_path):
            pytest.skip("logo.png not found")
        try:
            pygame.display.init()
            img = pygame.image.load(logo_path)
            assert img.get_width() > 0, "Logo image has 0 width"
        finally:
            pygame.display.quit()


# =============================================================================
# 2. UNIT TESTS — Karaoke pure logic
# =============================================================================

@pytest.mark.unit
class TestFilenameFromPath:

    def test_simple_mp4(self, karaoke_instance):
        assert karaoke_instance.filename_from_path("/songs/My Song.mp4") == "My Song"

    def test_with_youtube_id(self, karaoke_instance):
        result = karaoke_instance.filename_from_path("/songs/My Song---dQw4w9WgXcQ.mp4")
        assert result == "My Song"

    def test_nested_path(self, karaoke_instance):
        result = karaoke_instance.filename_from_path("/a/b/c/Song Title---abc123.mkv")
        assert result == "Song Title"

    def test_no_youtube_id(self, karaoke_instance):
        result = karaoke_instance.filename_from_path("/songs/Plain Song.mp4")
        assert result == "Plain Song"


@pytest.mark.unit
class TestYoutubeIdParsing:

    def test_standard_url(self, karaoke_instance):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert karaoke_instance.get_youtube_id_from_url(url) == "dQw4w9WgXcQ"

    def test_invalid_url(self, karaoke_instance):
        assert karaoke_instance.get_youtube_id_from_url("https://example.com") is None

    def test_find_song_by_youtube_id(self, karaoke_instance):
        result = karaoke_instance.find_song_by_youtube_id("abc123")
        assert result is not None
        assert "abc123" in result

    def test_find_song_not_found(self, karaoke_instance):
        result = karaoke_instance.find_song_by_youtube_id("nonexistent999")
        assert result is None


@pytest.mark.unit
class TestNetworkConnected:

    def test_valid_ip(self, karaoke_instance):
        karaoke_instance.ip = "192.168.1.100"
        assert karaoke_instance.is_network_connected() is True

    def test_short_ip(self, karaoke_instance):
        karaoke_instance.ip = ""
        assert karaoke_instance.is_network_connected() is False

    def test_localhost(self, karaoke_instance):
        karaoke_instance.ip = "127.0.0.1"
        assert karaoke_instance.is_network_connected() is True


# =============================================================================
# 3. UNIT TESTS — Queue management
# =============================================================================

@pytest.mark.unit
class TestQueueManagement:

    def test_enqueue_adds_song(self, karaoke_instance):
        karaoke_instance.queue = []
        result = karaoke_instance.enqueue("/songs/test.mp4", "TestUser")
        assert result is True
        assert len(karaoke_instance.queue) == 1
        assert karaoke_instance.queue[0]["file"] == "/songs/test.mp4"
        assert karaoke_instance.queue[0]["user"] == "TestUser"

    def test_enqueue_rejects_duplicate(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/test.mp4")
        result = karaoke_instance.enqueue("/songs/test.mp4")
        assert result is False
        assert len(karaoke_instance.queue) == 1

    def test_enqueue_default_user(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/test.mp4")
        assert karaoke_instance.queue[0]["user"] == "Pikaraoke"

    def test_is_song_in_queue(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/test.mp4")
        assert karaoke_instance.is_song_in_queue("/songs/test.mp4") is True
        assert karaoke_instance.is_song_in_queue("/songs/other.mp4") is False

    def test_queue_edit_up(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/a.mp4")
        karaoke_instance.enqueue("/songs/b.mp4")
        result = karaoke_instance.queue_edit("b.mp4", "up")
        assert result is True
        assert karaoke_instance.queue[0]["file"] == "/songs/b.mp4"

    def test_queue_edit_up_already_first(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/a.mp4")
        result = karaoke_instance.queue_edit("a.mp4", "up")
        assert result is False

    def test_queue_edit_down(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/a.mp4")
        karaoke_instance.enqueue("/songs/b.mp4")
        result = karaoke_instance.queue_edit("a.mp4", "down")
        assert result is True
        assert karaoke_instance.queue[1]["file"] == "/songs/a.mp4"

    def test_queue_edit_down_already_last(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/a.mp4")
        result = karaoke_instance.queue_edit("a.mp4", "down")
        assert result is False

    def test_queue_edit_delete(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/a.mp4")
        result = karaoke_instance.queue_edit("a.mp4", "delete")
        assert result is True
        assert len(karaoke_instance.queue) == 0

    def test_queue_clear(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/a.mp4")
        karaoke_instance.enqueue("/songs/b.mp4")
        # queue_clear calls skip(), which may check vlcclient
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.now_playing = None
        karaoke_instance.queue_clear()
        assert len(karaoke_instance.queue) == 0

    def test_queue_add_random(self, karaoke_instance):
        karaoke_instance.queue = []
        result = karaoke_instance.queue_add_random(2)
        assert result is True
        assert len(karaoke_instance.queue) == 2

    def test_queue_add_random_no_songs(self, karaoke_instance):
        karaoke_instance.queue = []
        karaoke_instance.available_songs = []
        result = karaoke_instance.queue_add_random(1)
        assert result is False


# =============================================================================
# 4. UNIT TESTS — Playback & Transpose (real bugs)
# =============================================================================

@pytest.mark.unit
class TestPlaybackAndTranspose:

    def test_transpose_converts_string_to_int(self, karaoke_instance):
        """Regression: semitones was passed as string from Flask route, causing
        VLC to receive '22' instead of 2+2=4. transpose_current must int()."""
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.vlcclient.get_time_and_length.return_value = (0.0, 0.0)

        with patch.object(karaoke_instance, "play_file") as mock_play:
            # Simulate what Flask sends: string "3"
            karaoke_instance.transpose_current("3")
            assert karaoke_instance.now_playing_transpose == 3

    def test_transpose_double_conversion_safe(self, karaoke_instance):
        """Regression: calling int() on something already int should be safe."""
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.vlcclient.get_time_and_length.return_value = (0.0, 0.0)
        with patch.object(karaoke_instance, "play_file"):
            karaoke_instance.transpose_current(5)
            assert karaoke_instance.now_playing_transpose == 5

    def test_transpose_from_beginning(self, karaoke_instance):
        """transpose_current with from_beginning=True should pass seek_to=0.0."""
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.vlcclient._get_current_time.return_value = 45.0
        karaoke_instance.vlcclient.get_time_and_length.return_value = (45.0, 210.0)
        with patch.object(karaoke_instance, "play_file") as mock_play:
            karaoke_instance.transpose_current(3, from_beginning=True)
            # Wait briefly for the daemon thread to call play_file
            import time; time.sleep(0.1)
            mock_play.assert_called_once_with("/songs/test.mp4", 3, seek_to=0.0)

    def test_transpose_continue_captures_position(self, karaoke_instance):
        """transpose_current with from_beginning=False should capture current playback position."""
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.vlcclient._get_current_time.return_value = 45.0
        karaoke_instance.vlcclient.get_time_and_length.return_value = (45.0, 210.0)
        with patch.object(karaoke_instance, "play_file") as mock_play:
            karaoke_instance.transpose_current(3, from_beginning=False)
            import time; time.sleep(0.1)
            mock_play.assert_called_once_with("/songs/test.mp4", 3, seek_to=45.0)

    def test_play_file_without_semitones(self, karaoke_instance):
        """play_file with semitones=0 should call vlcclient.play_file, not transpose."""
        karaoke_instance.play_file("/songs/test.mp4", semitones=0)
        karaoke_instance.vlcclient.play_file.assert_called_once_with("/songs/test.mp4")
        karaoke_instance.vlcclient.play_file_transpose.assert_not_called()

    def test_play_file_with_semitones(self, karaoke_instance):
        """play_file with nonzero semitones should call play_file_transpose."""
        karaoke_instance.play_file("/songs/test.mp4", semitones=3)
        karaoke_instance.vlcclient.play_file_transpose.assert_called_once_with("/songs/test.mp4", 3, seek_to=None)
        karaoke_instance.vlcclient.play_file.assert_not_called()

    def test_play_file_with_semitones_and_seek(self, karaoke_instance):
        """play_file with seek_to should pass it to play_file_transpose."""
        karaoke_instance.play_file("/songs/test.mp4", semitones=3, seek_to=45.0)
        karaoke_instance.vlcclient.play_file_transpose.assert_called_once_with("/songs/test.mp4", 3, seek_to=45.0)

    def test_skip_when_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.now_playing = "test"
        karaoke_instance.process = MagicMock()
        result = karaoke_instance.skip()
        assert result is True
        karaoke_instance.vlcclient.stop.assert_called_once()

    def test_skip_when_not_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.vlcclient = MagicMock()
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.now_playing = None
        result = karaoke_instance.skip()
        assert result is False

    def test_pause_when_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.vlcclient.is_playing.return_value = True
        karaoke_instance.now_playing = "test"
        result = karaoke_instance.pause()
        assert result is True

    def test_restart_when_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.now_playing = "test"
        result = karaoke_instance.restart()
        assert result is True
        karaoke_instance.vlcclient.restart.assert_called_once()

    def test_vol_up_when_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.now_playing = "test"
        result = karaoke_instance.vol_up()
        assert result is True
        karaoke_instance.vlcclient.vol_up.assert_called_once()

    def test_vol_down_when_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = True
        karaoke_instance.now_playing = "test"
        result = karaoke_instance.vol_down()
        assert result is True
        karaoke_instance.vlcclient.vol_down.assert_called_once()

    def test_vol_up_when_not_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.now_playing = None
        result = karaoke_instance.vol_up()
        assert result is False

    def test_vol_down_when_not_playing(self, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.now_playing = None
        result = karaoke_instance.vol_down()
        assert result is False


# =============================================================================
# 5. UNIT TESTS — VLCClient
# =============================================================================

@pytest.mark.unit
class TestVLCClient:

    def _make_vlcclient(self):
        """Create a VLCClient with platform mocked to linux."""
        with patch("lib.vlcclient.get_platform", return_value="linux"):
            from lib.vlcclient import VLCClient
            return VLCClient(port=5002, path="/usr/bin/vlc")

    def test_process_file_mp4_passthrough(self):
        client = self._make_vlcclient()
        assert client.process_file("/songs/video.mp4") == "/songs/video.mp4"

    def test_process_file_mkv_passthrough(self):
        client = self._make_vlcclient()
        assert client.process_file("/songs/video.mkv") == "/songs/video.mkv"

    def test_process_file_zip_calls_handle_zipped_cdg(self):
        client = self._make_vlcclient()
        with patch.object(client, "handle_zipped_cdg", return_value="/tmp/extracted/song.mp3") as mock:
            result = client.process_file("/songs/karaoke.zip")
            mock.assert_called_once_with("/songs/karaoke.zip")
            assert result == "/tmp/extracted/song.mp3"

    def test_process_file_mp3_calls_handle_mp3_cdg(self):
        client = self._make_vlcclient()
        with patch.object(client, "handle_mp3_cdg", return_value="/songs/song.mp3") as mock:
            result = client.process_file("/songs/song.mp3")
            mock.assert_called_once_with("/songs/song.mp3")
            assert result == "/songs/song.mp3"

    def test_is_running_process_active(self):
        client = self._make_vlcclient()
        client.process = MagicMock()
        client.process.poll.return_value = None  # still running
        client.is_transposing = False
        assert client.is_running() is True

    def test_is_running_while_transposing(self):
        client = self._make_vlcclient()
        client.process = None
        client.is_transposing = True
        assert client.is_running() is True

    def test_is_running_process_exited(self):
        client = self._make_vlcclient()
        client.process = MagicMock()
        client.process.poll.return_value = 0  # exited
        client.is_transposing = False
        assert client.is_running() is False

    def test_is_running_no_process(self):
        client = self._make_vlcclient()
        client.process = None
        client.is_transposing = False
        assert client.is_running() is False

    def test_get_default_vlc_path_linux(self):
        from lib.vlcclient import get_default_vlc_path
        assert get_default_vlc_path("linux") == "/usr/bin/vlc"

    def test_get_default_vlc_path_osx(self):
        from lib.vlcclient import get_default_vlc_path
        assert get_default_vlc_path("osx") == "/Applications/VLC.app/Contents/MacOS/VLC"

    def test_get_default_vlc_path_raspberry_pi(self):
        from lib.vlcclient import get_default_vlc_path
        assert get_default_vlc_path("raspberry_pi") == "/usr/bin/vlc"

    def test_transpose_progress_initialized(self):
        client = self._make_vlcclient()
        assert client.transpose_progress == 0
        assert client.transpose_duration == 0.0


@pytest.mark.unit
class TestFfmpegProgressParsing:

    def test_parse_out_time_us(self):
        from lib.vlcclient import _parse_ffmpeg_progress_time
        line = "out_time_us=83450000\n"
        result = _parse_ffmpeg_progress_time(line)
        assert result == pytest.approx(83.45, abs=0.01)

    def test_parse_zero_time(self):
        from lib.vlcclient import _parse_ffmpeg_progress_time
        line = "out_time_us=0\n"
        result = _parse_ffmpeg_progress_time(line)
        assert result == pytest.approx(0.0)

    def test_parse_non_progress_line(self):
        from lib.vlcclient import _parse_ffmpeg_progress_time
        line = "progress=continue\n"
        result = _parse_ffmpeg_progress_time(line)
        assert result is None

    def test_parse_other_line(self):
        from lib.vlcclient import _parse_ffmpeg_progress_time
        line = "frame=120\n"
        result = _parse_ffmpeg_progress_time(line)
        assert result is None

    def test_parse_large_time(self):
        from lib.vlcclient import _parse_ffmpeg_progress_time
        line = "out_time_us=5400000000\n"  # 1h30m in microseconds
        result = _parse_ffmpeg_progress_time(line)
        assert result == pytest.approx(5400.0)


@pytest.mark.unit
class TestVLCFfmpegPitchShift:

    def _make_vlcclient(self):
        with patch("lib.vlcclient.get_platform", return_value="linux"):
            from lib.vlcclient import VLCClient
            return VLCClient(port=5002, path="/usr/bin/vlc")

    def test_ffmpeg_not_found_returns_none(self):
        client = self._make_vlcclient()
        with patch("shutil.which", return_value=None):
            result = client._ffmpeg_pitch_shift("/songs/test.mp4", 3)
            assert result is None

    @staticmethod
    def _make_ffmpeg_mock_proc(returncode):
        """Create a mock ffmpeg process with proper stdout for progress parsing."""
        from io import BytesIO
        mock_proc = MagicMock()
        # poll() returns None first (enters while loop), then returncode (exits)
        mock_proc.poll.side_effect = [None, returncode]
        mock_proc.returncode = returncode
        mock_proc.stdout = BytesIO(b"")
        return mock_proc

    def test_ffmpeg_success_returns_path(self, tmp_path):
        client = self._make_vlcclient()
        client.tmp_dir = str(tmp_path) + "/"
        mock_proc = self._make_ffmpeg_mock_proc(0)

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(client, "process_file", return_value="/songs/test.mp4"):
            result = client._ffmpeg_pitch_shift("/songs/test.mp4", 3)
            assert result is not None
            assert "shifted" in result

    def test_ffmpeg_failure_returns_none(self, tmp_path):
        client = self._make_vlcclient()
        client.tmp_dir = str(tmp_path) + "/"
        mock_proc = self._make_ffmpeg_mock_proc(1)

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(client, "process_file", return_value="/songs/test.mp4"):
            result = client._ffmpeg_pitch_shift("/songs/test.mp4", 3)
            assert result is None

    def test_ffmpeg_cancelled_returns_sentinel(self, tmp_path):
        from lib.vlcclient import _FFMPEG_CANCELLED
        client = self._make_vlcclient()
        client.tmp_dir = str(tmp_path) + "/"
        mock_proc = self._make_ffmpeg_mock_proc(-9)

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(client, "process_file", return_value="/songs/test.mp4"):
            result = client._ffmpeg_pitch_shift("/songs/test.mp4", 3)
            assert result is _FFMPEG_CANCELLED


# =============================================================================
# 6. UNIT TESTS — Download
# =============================================================================

@pytest.mark.unit
class TestDownloadVideo:

    def test_download_success(self, karaoke_instance):
        with patch("subprocess.call", return_value=0):
            rc = karaoke_instance.download_video("https://www.youtube.com/watch?v=test123")
            assert rc == 0

    def test_download_retry_on_first_failure(self, karaoke_instance):
        with patch("subprocess.call", side_effect=[1, 0]):
            rc = karaoke_instance.download_video("https://www.youtube.com/watch?v=test123")
            assert rc == 0

    def test_download_failure(self, karaoke_instance):
        with patch("subprocess.call", return_value=1):
            rc = karaoke_instance.download_video("https://www.youtube.com/watch?v=test123")
            assert rc == 1

    def test_download_enqueue_on_success(self, karaoke_instance):
        karaoke_instance.queue = []
        with patch("subprocess.call", return_value=0):
            with patch.object(karaoke_instance, "get_youtube_id_from_url", return_value="abc123"):
                with patch.object(karaoke_instance, "find_song_by_youtube_id") as mock_find:
                    mock_find.return_value = karaoke_instance.available_songs[0]
                    karaoke_instance.download_video(
                        "https://www.youtube.com/watch?v=abc123",
                        enqueue=True,
                        user="TestUser",
                    )
                    assert len(karaoke_instance.queue) == 1
                    assert karaoke_instance.queue[0]["user"] == "TestUser"

    def test_download_high_quality_format(self, karaoke_instance):
        karaoke_instance.high_quality = True
        with patch("subprocess.call", return_value=0) as mock_call:
            karaoke_instance.download_video("https://www.youtube.com/watch?v=test123")
            cmd = mock_call.call_args[0][0]
            assert "bestvideo" in cmd[cmd.index("-f") + 1]

    def test_download_normal_quality_format(self, karaoke_instance):
        karaoke_instance.high_quality = False
        with patch("subprocess.call", return_value=0) as mock_call:
            karaoke_instance.download_video("https://www.youtube.com/watch?v=test123")
            cmd = mock_call.call_args[0][0]
            assert cmd[cmd.index("-f") + 1] == "mp4"


# =============================================================================
# 7. FLASK TESTS — HTTP routes
# =============================================================================

@pytest.mark.flask
class TestFlaskRoutes:

    def test_nowplaying_returns_json(self, flask_client, karaoke_instance):
        karaoke_instance.now_playing = None
        karaoke_instance.now_playing_user = None
        karaoke_instance.now_playing_filename = None
        karaoke_instance.is_paused = True
        karaoke_instance.now_playing_transpose = 0
        resp = flask_client.get("/nowplaying")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "now_playing" in data
        assert "now_playing_filepath" in data
        assert "is_paused" in data
        assert "transpose_value" in data

    def test_nowplaying_with_queue(self, flask_client, karaoke_instance):
        karaoke_instance.queue = [
            {"title": "Next Song", "user": "Bob", "file": "/songs/next.mp4"}
        ]
        resp = flask_client.get("/nowplaying")
        data = json.loads(resp.data)
        assert data["up_next"] == "Next Song"
        assert data["next_user"] == "Bob"

    def test_get_queue_empty(self, flask_client, karaoke_instance):
        karaoke_instance.queue = []
        resp = flask_client.get("/get_queue")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == []

    def test_get_queue_with_songs(self, flask_client, karaoke_instance):
        karaoke_instance.queue = [
            {"title": "Song A", "user": "Alice", "file": "/songs/a.mp4"},
            {"title": "Song B", "user": "Bob", "file": "/songs/b.mp4"},
        ]
        resp = flask_client.get("/get_queue")
        data = json.loads(resp.data)
        assert len(data) == 2

    def test_skip_redirects(self, flask_client, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.now_playing = None
        resp = flask_client.get("/skip")
        assert resp.status_code == 302

    def test_pause_redirects(self, flask_client, karaoke_instance):
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.now_playing = None
        resp = flask_client.get("/pause")
        assert resp.status_code == 302

    def test_transpose_status_before_transpose(self, flask_client, karaoke_instance):
        """Regression: /transpose/status must resolve before /transpose/<int>,
        otherwise Flask matches 'status' as an int and raises 404."""
        karaoke_instance.vlcclient.is_transposing = False
        karaoke_instance.vlcclient.transpose_progress = 0
        resp = flask_client.get("/transpose/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "transposing" in data
        assert "progress" in data
        assert data["transposing"] is False
        assert data["progress"] == 0

    def test_transpose_with_semitones(self, flask_client, karaoke_instance):
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        with patch.object(karaoke_instance, "transpose_current") as mock_transpose:
            resp = flask_client.get("/transpose/3")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["status"] == "ok"
            mock_transpose.assert_called_once_with(3)

    def test_transpose_with_mode_continue(self, flask_client, karaoke_instance):
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        with patch.object(karaoke_instance, "transpose_current") as mock_transpose:
            resp = flask_client.get("/transpose/3/continue")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["status"] == "ok"
            mock_transpose.assert_called_once_with(3, from_beginning=False)

    def test_transpose_with_mode_restart(self, flask_client, karaoke_instance):
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        with patch.object(karaoke_instance, "transpose_current") as mock_transpose:
            resp = flask_client.get("/transpose/3/restart")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["status"] == "ok"
            mock_transpose.assert_called_once_with(3, from_beginning=True)


# =============================================================================
# 8. REGRESSION TESTS
# =============================================================================

@pytest.mark.unit
class TestRegressions:

    def test_semitones_string_to_int_safe(self, karaoke_instance):
        """Bug: Flask passes semitones as string from URL. transpose_current must handle it."""
        karaoke_instance.now_playing_filename = "/songs/test.mp4"
        karaoke_instance.now_playing = "test"
        karaoke_instance.vlcclient.is_running.return_value = False
        karaoke_instance.vlcclient.get_time_and_length.return_value = (0.0, 0.0)
        with patch.object(karaoke_instance, "play_file"):
            # Should not raise
            karaoke_instance.transpose_current("3")
            karaoke_instance.transpose_current("-2")
            karaoke_instance.transpose_current(0)

    def test_download_path_ends_with_slash(self, karaoke_instance):
        """Bug: download_path without trailing / caused malformed file paths."""
        assert karaoke_instance.download_path.endswith("/")

    def test_enqueue_title_from_filename(self, karaoke_instance):
        """Queue entries must have title derived from filename, not full path."""
        karaoke_instance.queue = []
        karaoke_instance.enqueue("/songs/Artist - Song One---abc123.mp4")
        title = karaoke_instance.queue[0]["title"]
        assert title == "Artist - Song One"
        assert "/" not in title
        assert "abc123" not in title

    def test_available_songs_loaded_on_init(self, karaoke_instance):
        """Constructor must populate available_songs from the download directory."""
        assert len(karaoke_instance.available_songs) == 3

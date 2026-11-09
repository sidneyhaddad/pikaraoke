import gettext
import os
import sys
import tempfile

import pytest
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def song_dir(tmp_path):
    """Temporary directory pre-populated with fake song files."""
    songs = [
        "Artist - Song One---abc123.mp4",
        "Band - Song Two---def456.mp4",
        "Solo - Track Three---ghi789.mkv",
    ]
    for name in songs:
        (tmp_path / name).write_text("fake video data")
    # Ensure path ends with /
    return str(tmp_path) + "/"


@pytest.fixture
def karaoke_instance(song_dir):
    """Create a Karaoke instance with heavy side-effects mocked out."""
    patches = [
        patch("karaoke.get_platform", return_value="linux"),
        patch("karaoke.check_output", side_effect=_mock_check_output),
        patch("subprocess.call", return_value=0),
        patch("karaoke.qrcode.QRCode", return_value=_mock_qr()),
        patch("karaoke.vlcclient.VLCClient", return_value=MagicMock()),
        patch("karaoke.gettext.translation", return_value=gettext.NullTranslations()),
    ]
    for p in patches:
        p.start()

    from karaoke import Karaoke
    k = Karaoke(
        port=5000,
        download_path=song_dir,
        hide_splash_screen=True,
        youtubedl_path="/usr/local/bin/yt-dlp",
    )

    yield k

    for p in patches:
        p.stop()


@pytest.fixture
def flask_client(karaoke_instance):
    """Flask test client wired to a mocked Karaoke instance."""
    import app as app_module
    app_module.k = karaoke_instance
    app_module.app.config["TESTING"] = True
    app_module.admin_password = None
    with app_module.app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_check_output(cmd, *args, **kwargs):
    """Return plausible output for subprocess.check_output calls during init."""
    if cmd[0] == "hostname":
        return b"192.168.1.100 "
    if "version" in cmd or "--version" in cmd:
        return b"2025.01.01"
    return b""


def _mock_qr():
    qr = MagicMock()
    img = MagicMock()
    qr.make_image.return_value = img
    return qr

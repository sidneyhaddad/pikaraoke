# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PiKaraoke is a KTV-style karaoke system with a Flask web server that multiple devices connect to via browser, a Pygame-rendered splash screen for TV output, and VLC or OMXPlayer for media playback. Designed primarily for Raspberry Pi but runs on Linux, macOS, and Windows.

The system runs on a **Raspberry Pi** (hostname: `myvikaraoke`, user: `shaddad`) with:
- 94,000+ songs stored in `/home/shaddad/Documents/pikaraoke-songs/`
- Song metadata cached in `karaoke.db` (SQLite), with filesystem scan fallback
- A companion WebSocket server (`pikaraoke-ws.service`) on port 80
- The karaoke app itself on port 5000

## Running the Application

```bash
# Basic run
python3 app.py

# With common options (see start.sh for local config)
python3 app.py -y /path/to/yt-dlp --download-path ~/pikaraoke-songs/ --high-quality

# Via systemd (production)
sudo systemctl restart pikaraoke.service

# Via start script (includes pre-flight checks)
./start.sh
```

Key CLI flags: `-p PORT`, `-d DOWNLOAD_PATH`, `-y YOUTUBEDL_PATH`, `-v VOLUME`, `--use-vlc`, `--use-omxplayer`, `--admin-password`, `--developer-mode`, `--high-quality`, `--show-overlay`.

## Systemd Services

```
# Main karaoke service — runs pre-flight checks then app.py
pikaraoke.service          → /etc/systemd/system/pikaraoke.service
  ExecStartPre = preflight_check.py  (smoke tests on TV screen)
  ExecStart    = app.py

# Companion WebSocket server
pikaraoke-ws.service       → runs server.py from /home/shaddad/Documents/pikaraoke-ws/
  Listens on port 80, proxies to karaoke on port 5000
```

After editing the service file:
```bash
sudo systemctl daemon-reload
sudo systemctl restart pikaraoke.service
```

## Installation

```bash
pip3 install -r requirements.txt
pip3 install --upgrade yt_dlp

# Test dependencies
pip3 install pytest pytest-mock
```

For Raspberry Pi, run `./setup-pi.sh` (installs system deps, patches VLC for sudo, configures GPU memory).

## Testing

```bash
# Run all tests (93 tests)
python3 -m pytest tests/ -v

# Smoke tests only — real system checks, no mocks (25 tests)
python3 -m pytest tests/ -m smoke -v

# Unit tests only — fast, fully mocked (60 tests)
python3 -m pytest tests/ -m unit -v

# Flask route tests only (8 tests)
python3 -m pytest tests/ -m flask -v
```

### Test File Structure

```
pytest.ini              — markers: smoke, unit, flask
tests/__init__.py       — package marker
tests/conftest.py       — shared fixtures (mocked Karaoke, Flask client, temp song dir)
tests/test_pikaraoke.py — all tests in one file
```

### Pre-flight Check System (`preflight_check.py`)

Runs automatically before karaoke starts (via `ExecStartPre` in systemd or `start.sh`):

1. Initializes pygame fullscreen on the TV
2. Runs each smoke test one by one, showing `[OK]` / `[FAIL]` live on screen
3. If yt-dlp fails → auto-upgrades via pip, re-runs tests
4. All pass → shows green "All Checks Passed!" for 2s, then starts karaoke
5. Any fail → shows red "Pre-flight Check Failed" with details, holds 60s, exits 1

### Smoke Test Coverage (25 tests)

| Category | What it tests |
|---|---|
| Dependencies (4) | yt-dlp installed + recent version, ffmpeg on PATH, VLC on PATH |
| FFmpeg pitch shift (4) | Generates real WAV, shifts pitch up/down, shifts audio in MP4 with video copy |
| VLC (2) | `vlc --version` runs, HTTP interface available |
| CDG extraction (2) | Creates zip with .mp3+.cdg, extracts, verifies; `/tmp/pikaraoke/extracted` writable |
| Network (2) | Local IP is not 127.0.0.1, hostname resolves |
| Song library (3) | Scans media files by extension, handles subdirs, download dir exists |
| Pygame (3) | Display init, font rendering, logo image loading |
| Permissions (2) | `/tmp/pikaraoke/` writable, download dir writable |
| Translations (1) | Compiled .mo files exist for all LANGUAGES |
| Root check (1) | Not running as root (VLC refuses) |

### Mocking Strategy for Unit Tests

The `Karaoke` constructor does heavy initialization. The `conftest.py` fixture mocks:
- `get_platform()` → `"linux"`
- `check_output(["hostname", "-I"])` → `b"192.168.1.100 "`
- `check_output([ytdlp, "--version"])` → `b"2025.01.01"`
- `subprocess.call(["pkill"...])` → returns 0
- `qrcode.QRCode` → MagicMock
- `vlcclient.VLCClient` → MagicMock
- `gettext.translation` → NullTranslations
- `hide_splash_screen=True` → skips pygame init

### Known Test Gotchas

- `skip()`, `pause()`, `restart()` log `self.now_playing` — must set it to a string before calling, or set `vlcclient.is_running.return_value = False` to take the "not playing" branch
- `queue_clear()` calls `self.skip()` internally — same gotcha applies
- The `queue` attribute is a class-level list — always reset to `[]` at the start of queue tests
- `MagicMock().is_running()` returns a truthy MagicMock by default — explicitly set `.return_value`

## Translations (Babel)

```bash
# Extract strings
pybabel extract -F babel.cfg -o messages.pot .

# Update existing translations
pybabel update -i messages.pot -d translations

# Compile translations (required — smoke test checks for .mo files)
pybabel compile -d translations
```

Translation files: `translations/pt_BR/` and `translations/zh_Hans_CN/` (note: constants.py says `zh_CN` but the directory is `zh_Hans_CN`).

See `TRANSLATION.md` for adding new languages.

## Architecture

### Component Relationships

```
Browser → Flask (app.py) → Karaoke engine (karaoke.py) → VLCClient/OMXClient → VLC/OMXPlayer
                                                        → Pygame (splash screen / TV display)
```

- **`app.py`** — Flask routes + CherryPy WSGI server. All HTTP endpoints are here; they delegate business logic to a shared `Karaoke` instance. Global `k` variable holds the Karaoke instance.
- **`karaoke.py`** — Core engine: song library, queue management, playback control, splash screen rendering, YouTube download coordination, and the main event loop (`run()`).
- **`lib/vlcclient.py`** — Communicates with VLC via its HTTP API (default port 5002). Handles pitch shifting via ffmpeg (preferred) or VLC's Speex resampler (fallback). Contains `_FFMPEG_CANCELLED` sentinel for thread-safe cancellation.
- **`lib/omxclient.py`** — OMXPlayer client (Raspberry Pi alternative to VLC).
- **`lib/get_platform.py`** — Returns `"raspberry_pi"`, `"linux"`, `"osx"`, or `"windows"`. Detection: `os.uname()[4][:3] == "arm"` for Pi.
- **`constants.py`** — `VERSION`, `LANGUAGES` dict, `COLORS` dict (white/blue/green).
- **`templates/`** — Jinja2 templates using Bulma CSS. `base.html` provides the nav shell.
- **`translations/`** — Babel `.po`/`.mo` files (zh_Hans_CN, pt_BR).
- **`preflight_check.py`** — Pre-flight smoke test runner with live pygame display.
- **`start.sh`** — Shell script: runs preflight_check.py, then app.py.

### Request / Playback Flow

1. Browser POSTs to `/enqueue` → `karaoke.enqueue(filepath, user)`
2. `karaoke.run()` event loop detects the queue and calls `karaoke.play_file(filepath, semitones)`
3. `play_file` kills any running player, then invokes `vlcclient.play_file()` or `vlcclient.play_file_transpose()`
4. VLC is controlled via HTTP on `localhost:5002`; pitch shift uses ffmpeg `asetrate+atempo` filter (high quality) with VLC Speex fallback
5. `/nowplaying` JSON endpoint is polled by the browser for AJAX updates

### Pitch Shift / Transpose Flow

1. Browser hits `/transpose/<int:semitones>` → `karaoke.transpose_current(semitones)`
2. `transpose_current` converts semitones to int (regression fix: Flask passes string from URL)
3. Spawns a daemon thread calling `play_file(filename, semitones)`
4. `play_file` → `vlcclient.play_file_transpose(file, semitones)`
5. `play_file_transpose` → `_ffmpeg_pitch_shift(file, semitones)`:
   - Calculates ratio: `2^(semitones/12)`
   - Builds ffmpeg filter: `asetrate=44100*ratio,aresample=44100,atempo=1/ratio`
   - For video: `-vcodec copy -af <filter>` (copies video, shifts audio only)
   - For audio (mp3/cdg): shifts audio, copies .cdg alongside
   - Saves to `/tmp/pikaraoke/shifted.*`
6. If ffmpeg fails → falls back to VLC `scaletempo_pitch` filter
7. If ffmpeg killed by newer request → returns `_FFMPEG_CANCELLED`, thread exits silently
8. `is_transposing` flag + 2s Timer prevents `is_running()` from returning False during switch

### Song Library

- Primary: SQLite database at `<download_path>/karaoke.db` (table `songs`, column `filepath`)
- Fallback: filesystem scan via `pathlib.Path.rglob('*.*')` for extensions: `.mp4`, `.mp3`, `.zip`, `.mkv`, `.avi`, `.webm`, `.mov`
- Filenames are source of truth: `Artist - Song Title---YouTubeID.ext`
- `filename_from_path()` strips path, extension, and `---<id>` suffix

### CDG Karaoke Files

- `.zip` files: extracted to `/tmp/pikaraoke/extracted/`, must contain matching `.mp3` + `.cdg`
- `.mp3` files: look for matching `.cdg` in same directory (case-insensitive)
- Other extensions (`.mp4`, `.mkv`, etc.): passed through directly to VLC

### Key Design Points

- No persistent config files — all configuration is via CLI arguments passed into the `Karaoke` constructor
- The Flask app and the Pygame display loop run concurrently; Flask routes access `karaoke` state directly (no REST abstraction layer)
- Song library is loaded from the filesystem at startup and rescanned on demand; filenames are the source of truth for titles
- Admin mode is toggled by passing `--admin-password`; session cookie gates protected routes
- `yt_dlp` is invoked as a subprocess (not the Python API) for downloads
- The `queue` is a class-level list of dicts: `{"user": str, "file": str, "title": str}`
- Flask route ordering matters: `/transpose/status` must be defined before `/transpose/<int:semitones>` or "status" gets parsed as an int and 404s

## Known Bugs and Fixes Applied

- **`logging.warn()` deprecated**: All calls in `karaoke.py` changed to `logging.warning()` (9 occurrences)
- **Invalid escape sequences**: `app.py` Windows paths `"~\pikaraoke\songs"` → `r"~\pikaraoke\songs"` (raw strings)
- **Semitones string→int**: `transpose_current()` calls `int(semitones)` — Flask passes URL params as strings
- **Port conflict on restart**: Old VLC/app processes may hold port 5000/5002. Constructor calls `pkill -f vlc`. If service restart fails with "Port 5000 in use", kill the stale process first.

## File Paths (Production)

```
/home/shaddad/Documents/pikaraoke/          — project root
/home/shaddad/Documents/pikaraoke-songs/    — song download directory (94k+ songs)
/home/shaddad/Documents/pikaraoke-songs/karaoke.db — song database
/home/shaddad/.local/bin/yt-dlp             — yt-dlp binary
/usr/bin/vlc                                — VLC binary
/tmp/pikaraoke/                             — temp dir for CDG extraction and pitch-shifted files
/tmp/pikaraoke/extracted/                   — CDG zip extraction target
/tmp/pikaraoke/shifted.*                    — ffmpeg pitch-shifted output files
/etc/systemd/system/pikaraoke.service       — systemd unit file
```

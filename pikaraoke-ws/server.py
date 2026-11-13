"""
PiKaraoke WebSocket Bridge — server.py
FastAPI backend que conecta browsers ao PiKaraoke via WebSocket em tempo real.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pikaraoke-ws")

# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PiKaraoke WebSocket Bridge")
    p.add_argument("--pikaraoke-url",
                    default=os.environ.get("PIKARAOKE_URL", "http://localhost:5000"),
                    help="PiKaraoke Flask URL")
    p.add_argument("--download-path",
                    default=os.environ.get("DOWNLOAD_PATH", "/home/shaddad/Documents/pikaraoke-songs/"))
    p.add_argument("--ytdlp-path",
                    default=os.environ.get("YTDLP_PATH", "/home/shaddad/.local/bin/yt-dlp"))
    p.add_argument("--db-path",
                    default=os.environ.get("DB_PATH", "/home/shaddad/Documents/pikaraoke-songs/karaoke.db"))
    p.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "5001")))
    return p.parse_args()


args = parse_args()
PIKARAOKE_URL = args.pikaraoke_url.rstrip("/")
DOWNLOAD_PATH = args.download_path
YTDLP_PATH = args.ytdlp_path
DB_PATH = args.db_path
PORT = args.port

# ---------------------------------------------------------------------------
# ConnectionManager — WebSocket pool
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)
        log.info(f"WS conectado. Total: {len(self.active)}")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.active = [c for c in self.active if c is not ws]
        log.info(f"WS desconectado. Total: {len(self.active)}")

    async def broadcast(self, message: dict):
        payload = json.dumps(message, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def send_to(self, ws: WebSocket, message: dict):
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            await self.disconnect(ws)


manager = ConnectionManager()

# ---------------------------------------------------------------------------
# StateTracker — polling PiKaraoke + broadcast de mudanças
# ---------------------------------------------------------------------------

class StateTracker:
    def __init__(self):
        self._nowplaying: dict = {}
        self._queue: list = []
        self._url: str = PIKARAOKE_URL
        self._client = httpx.AsyncClient(timeout=3.0)
        self._last_now_playing: str = ""  # título da última música detectada
        self._current_play_id: int = 0   # ID do registro atual no play_history
        self._current_playing_filepath: str = ""  # filepath da música atual
        self._current_song_title: str = ""   # título da música (da tabela songs)
        self._current_song_artist: str = ""  # artista da música (da tabela songs)
        self._pending_transpositions: dict = {}  # filepath → semitones a aplicar
        self._current_semitones: int = 0      # semitom efetivo da música atual
        self._cached_position: float = 0.0   # última posição conhecida (cache)
        self._cached_duration: float = 0.0   # duração da música atual (cache)
        self._system_volume: int = 50         # volume do sistema em % (cache)
        self._current_transpose_job_id: str = ""  # job de transposição em andamento

    def update_url(self, url: str):
        if url != self._url:
            log.info(f"PiKaraoke URL atualizada: {url}")
            self._url = url

    async def poll_nowplaying(self) -> bool:
        try:
            r = await self._client.get(f"{self._url}/nowplaying")
            text = r.text.strip()
            if not text:
                return False
            data = json.loads(text)
            if data != self._nowplaying:
                new_title = data.get("now_playing", "") if isinstance(data, dict) else ""
                # Nova música começou a tocar
                if new_title and new_title != self._last_now_playing:
                    user = (data.get("now_playing_user") or "") if isinstance(data, dict) else ""
                    # Tenta obter filepath da resposta /nowplaying (mais confiável)
                    filepath = (data.get("now_playing_filepath") or "") if isinstance(data, dict) else ""
                    # Fallback: tenta do primeiro item da fila (estado antigo)
                    if not filepath and self._queue:
                        q0 = self._queue[0]
                        if isinstance(q0, dict) and q0.get("title") == new_title:
                            filepath = q0.get("file", "")
                    # Detecta semitom se arquivo já for transposto (ex: song_Tom+3.zip)
                    initial_semitones = await asyncio.to_thread(db_get_semitone_for_transposed_file, filepath) if filepath else 0
                    play_id = await asyncio.to_thread(db_record_play, new_title, filepath, user, initial_semitones)
                    self._current_play_id = play_id
                    self._current_playing_filepath = filepath
                    self._current_semitones = initial_semitones
                    # Busca título e artista reais na tabela songs
                    song_info = await asyncio.to_thread(db_get_song_by_filepath, filepath) if filepath else None
                    self._current_song_title = (song_info.get("title") or new_title) if song_info else new_title
                    self._current_song_artist = (song_info.get("artist") or "") if song_info else ""
                    # Auto-aplica transposição pendente para esta música
                    if filepath and filepath in self._pending_transpositions:
                        semitones = self._pending_transpositions.pop(filepath)
                        if semitones != 0:
                            try:
                                await self._client.get(f"{self._url}/transpose/{semitones}")
                                await asyncio.to_thread(db_update_play_semitones, play_id, semitones)
                                log.info(f"Auto-transpose {semitones:+d} aplicado para '{new_title}'")
                            except Exception as e:
                                log.warning(f"Auto-transpose falhou: {e}")
                # Só atualiza _last_now_playing quando há título real (evita regravar mesma música)
                if new_title:
                    self._last_now_playing = new_title
                elif not new_title:
                    # Música acabou — reseta semitom e estado da música atual
                    self._current_semitones = 0
                    self._current_playing_filepath = ""
                    self._current_song_title = ""
                    self._current_song_artist = ""
                self._nowplaying = data
                # Atualiza filepath sempre que disponível (cobre o caso de música já tocando antes do WS iniciar)
                np_filepath = (data.get("now_playing_filepath") or "") if isinstance(data, dict) else ""
                if np_filepath and not self._current_playing_filepath:
                    self._current_playing_filepath = np_filepath
                # Busca posição/duração uma única vez ao detectar mudança de estado
                try:
                    pos_r = await self._client.get(f"{self._url}/playback_pos")
                    pos_data = pos_r.json()
                    self._cached_position = pos_data.get("position", 0.0)
                    self._cached_duration = pos_data.get("duration", 0.0)
                except Exception:
                    pass
                broadcast_payload = {
                    **data,
                    "now_playing_filepath": self._current_playing_filepath,
                    "now_playing_title": self._current_song_title,
                    "now_playing_artist": self._current_song_artist,
                    "now_playing_semitones": self._current_semitones,
                    "position": self._cached_position,
                    "duration": self._cached_duration,
                }
                await manager.broadcast({"type": "now_playing", "payload": broadcast_payload})
                return True
        except Exception as e:
            log.debug(f"nowplaying poll error: {e}")
        return False

    async def poll_playback_position(self) -> bool:
        """Fetch current playback position from VLC and broadcast to all clients."""
        try:
            np = self._nowplaying
            # Só faz poll se houver música tocando e não pausada
            if not np or not (np.get("now_playing") if isinstance(np, dict) else False):
                return False
            if isinstance(np, dict) and np.get("is_paused"):
                return False
            r = await self._client.get(f"{self._url}/playback_pos")
            pos_data = r.json()
            pos = pos_data.get("position", 0.0)
            dur = pos_data.get("duration", 0.0)
            # Só broadcast se mudou significativamente (>0.5s de diferença)
            if abs(pos - self._cached_position) > 0.5 or abs(dur - self._cached_duration) > 0.5:
                self._cached_position = pos
                self._cached_duration = dur
                await manager.broadcast({
                    "type": "seek_update",
                    "payload": {"position": pos, "duration": dur},
                })
                return True
        except Exception as e:
            log.debug(f"playback_pos poll error: {e}")
        return False

    async def poll_queue(self) -> bool:
        try:
            r = await self._client.get(f"{self._url}/get_queue")
            data = r.json()
            if not isinstance(data, list):
                return False
            if data != self._queue:
                self._queue = data
                await manager.broadcast({"type": "queue_updated", "payload": data})
                return True
        except Exception as e:
            log.debug(f"queue poll error: {e}")
        return False

    def get_current_state(self) -> dict:
        now_playing_data = (
            {
                **self._nowplaying,
                "now_playing_filepath": self._current_playing_filepath,
                "now_playing_title": self._current_song_title,
                "now_playing_artist": self._current_song_artist,
                "now_playing_semitones": self._current_semitones,
                "position": self._cached_position,
                "duration": self._cached_duration,
            }
            if self._nowplaying else {}
        )
        return {
            "type": "full_state",
            "payload": {
                "now_playing": now_playing_data,
                "queue": self._queue,
                "system_volume": self._system_volume,
                "transpose_job_id": self._current_transpose_job_id or None,
                "downloads": {
                    dl_id: {k: v for k, v in info.items()}
                    for dl_id, info in download_manager.downloads.items()
                    if info.get("status") in ("downloading", "complete")
                },
            },
        }


tracker = StateTracker()

# ---------------------------------------------------------------------------
# Volume do sistema (pactl / amixer)
# ---------------------------------------------------------------------------

def get_system_volume() -> int:
    """Retorna volume do sistema em % (0-100)."""
    import subprocess as _sp
    # PulseAudio
    try:
        r = _sp.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                    capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            m = re.search(r"(\d+)%", r.stdout)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    # ALSA fallback
    try:
        r = _sp.run(["amixer", "sget", "Master"],
                    capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            m = re.search(r"\[(\d+)%\]", r.stdout)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 50


def set_system_volume(percent: int) -> int:
    """Define volume do sistema em % (0-100). Retorna o valor aplicado."""
    import subprocess as _sp
    percent = max(0, min(100, percent))
    # PulseAudio
    try:
        r = _sp.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
                    capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return percent
    except Exception:
        pass
    # ALSA fallback
    try:
        _sp.run(["amixer", "sset", "Master", f"{percent}%"],
                capture_output=True, text=True, timeout=3)
    except Exception:
        pass
    return percent


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

async def polling_loop():
    log.info("Polling loop iniciado (intervalo: 0.8s)")
    tick = 0
    while True:
        # Sequencial: nowplaying primeiro para capturar queue antes da atualização
        try:
            await tracker.poll_nowplaying()
        except Exception as e:
            log.debug(f"poll_nowplaying error: {e}")
        try:
            await tracker.poll_queue()
        except Exception as e:
            log.debug(f"poll_queue error: {e}")
        # Sincroniza posição de playback a cada ~2.4s (3 ticks × 0.8s)
        tick += 1
        if tick % 3 == 0:
            try:
                await tracker.poll_playback_position()
            except Exception as e:
                log.debug(f"poll_playback_position error: {e}")
        await asyncio.sleep(0.8)

# ---------------------------------------------------------------------------
# DownloadManager — yt-dlp com progresso em tempo real
# ---------------------------------------------------------------------------

# [download]  45.3% of   23.45MiB at    1.23MiB/s ETA 00:12
# [download]  97.7% of ~   6.04MiB at  446.79KiB/s ETA 00:01 (frag 73/76)
PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*(\S+)\s+at\s+(\S+)\s+ETA\s+(\S+)"
)
DEST_RE    = re.compile(r"\[download\] Destination: (.+)")
ALREADY_RE = re.compile(r"\[download\] (.+) has already been downloaded")
MERGE_RE   = re.compile(r'\[(?:Merger|ffmpeg)\] Merging formats into "(.+)"')


class DownloadManager:
    def __init__(self):
        self.downloads: dict[str, dict] = {}
        self._last_progress_time: dict[str, float] = {}

    async def start(self, url: str, user: str, enqueue_after: bool, title_hint: str = "") -> str:
        dl_id = str(uuid.uuid4())[:8]
        self.downloads[dl_id] = {
            "status": "downloading",
            "percent": 0.0,
            "url": url,
            "title": title_hint or url,
            "user": user,
        }
        await manager.broadcast({
            "type": "download_started",
            "payload": {"id": dl_id, "title": title_hint or url, "user": user, "url": url},
        })
        asyncio.create_task(self._run(dl_id, url, user, enqueue_after))
        return dl_id

    async def _run(self, dl_id: str, url: str, user: str, enqueue_after: bool):
        dl_template = os.path.join(DOWNLOAD_PATH, "%(title)s---%(id)s.%(ext)s")
        cmd = [
            YTDLP_PATH,
            "-f", "bestvideo[ext!=webm][height<=1080]+bestaudio[ext!=webm]/best[ext!=webm]",
            "--write-sub",
            "--newline",  # CRÍTICO: uma linha por update de progresso
            "-o", dl_template,
            url,
        ]
        log.info(f"[{dl_id}] Iniciando download: {url}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            downloaded_filepath: Optional[str] = None
            # Rastreia streams para escalar o progresso suavemente
            # yt-dlp baixa vídeo (0→100%) e áudio (0→100%) separadamente antes de mesclar.
            # Escalamos: stream 0 = 0→50%, stream 1 = 50→99%; 100% só em download_complete.
            stream_idx = 0       # índice do stream atual (0 = vídeo, 1 = áudio)
            prev_pct = -1.0      # último percentual visto (para detectar reset)

            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                log.debug(f"[yt-dlp {dl_id}] {line}")

                m = PROGRESS_RE.search(line)
                if m:
                    pct = float(m.group(1))
                    # Detecta reset: progresso caiu muito (novo stream começou)
                    if prev_pct > 80.0 and pct < 20.0:
                        stream_idx += 1
                        log.debug(f"[{dl_id}] Novo stream detectado (idx={stream_idx})")
                    prev_pct = pct

                    # Escala para evitar ir a 100% antes de estar completo
                    if stream_idx == 0:
                        display_pct = pct * 0.5          # 0 → 50
                    elif stream_idx == 1:
                        display_pct = 50.0 + pct * 0.49  # 50 → ~99
                    else:
                        display_pct = 99.0
                    display_pct = min(display_pct, 99.0)

                    self.downloads[dl_id]["percent"] = display_pct
                    now = asyncio.get_event_loop().time()
                    if now - self._last_progress_time.get(dl_id, 0) >= 2.0:
                        self._last_progress_time[dl_id] = now
                        await manager.broadcast({
                            "type": "download_progress",
                            "payload": {
                                "id": dl_id,
                                "percent": display_pct,
                                "size": m.group(2),
                                "speed": m.group(3),
                                "eta": m.group(4),
                            },
                        })
                    continue

                d = DEST_RE.search(line)
                if d:
                    downloaded_filepath = d.group(1).strip()

                al = ALREADY_RE.search(line)
                if al:
                    downloaded_filepath = al.group(1).strip()

                mg = MERGE_RE.search(line)
                if mg:
                    downloaded_filepath = mg.group(1).strip()

            await proc.wait()
            rc = proc.returncode

            if rc == 0:
                self.downloads[dl_id]["status"] = "complete"
                self.downloads[dl_id]["percent"] = 100.0

                # Fallback: busca pelo ID do vídeo no nome do arquivo
                if not downloaded_filepath or not os.path.exists(downloaded_filepath):
                    vid_id = url.split("v=")[-1].split("&")[0] if "v=" in url else url.split("/")[-1]
                    if vid_id:
                        matches = [
                            os.path.join(DOWNLOAD_PATH, f)
                            for f in os.listdir(DOWNLOAD_PATH)
                            if f"---{vid_id}." in f and os.path.isfile(os.path.join(DOWNLOAD_PATH, f))
                        ]
                        if matches:
                            downloaded_filepath = max(matches, key=os.path.getmtime)
                            log.info(f"[{dl_id}] Filepath recuperado via fallback: {downloaded_filepath}")

                log.info(f"[{dl_id}] Download completo: {downloaded_filepath}")

                # Diz ao PiKaraoke para rescanear a biblioteca
                async with httpx.AsyncClient(timeout=5.0) as client:
                    try:
                        await client.get(f"{tracker._url}/refresh")
                    except Exception as e:
                        log.warning(f"[{dl_id}] /refresh falhou: {e}")

                enqueued = False
                if enqueue_after and downloaded_filepath and os.path.exists(downloaded_filepath):
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        try:
                            r = await client.get(
                                f"{tracker._url}/enqueue",
                                params={"song": downloaded_filepath, "user": user},
                            )
                            result = json.loads(r.text)
                            enqueued = result.get("success", False)
                        except Exception as e:
                            log.warning(f"[{dl_id}] enqueue falhou: {e}")

                # Insere no banco de dados para futuros usuários encontrarem
                title_hint = self.downloads[dl_id]["title"]
                if downloaded_filepath:
                    song_id = await asyncio.to_thread(
                        db_insert_downloaded_song, downloaded_filepath, title_hint
                    )
                    # Detecta tom musical em background (não bloqueia o download_complete)
                    if song_id:
                        asyncio.create_task(_detect_and_update_key(downloaded_filepath, song_id))

                # Poll da fila ANTES de notificar o cliente (música já aparece na fila)
                await asyncio.sleep(0.5)
                await tracker.poll_queue()

                await manager.broadcast({
                    "type": "download_complete",
                    "payload": {
                        "id": dl_id,
                        "title": title_hint,
                        "filepath": downloaded_filepath,
                        "enqueued": enqueued,
                    },
                })

            else:
                raise RuntimeError(f"yt-dlp saiu com código {rc}")

        except Exception as e:
            log.error(f"[{dl_id}] Erro no download: {e}")
            self.downloads[dl_id]["status"] = "error"
            await manager.broadcast({
                "type": "download_error",
                "payload": {
                    "id": dl_id,
                    "error": str(e),
                    "title": self.downloads[dl_id]["title"],
                },
            })
        finally:
            self._last_progress_time.pop(dl_id, None)


download_manager = DownloadManager()

# ---------------------------------------------------------------------------
# DB search — sqlite3 direto no karaoke.db
# ---------------------------------------------------------------------------

import unicodedata

def _normalize(text: Optional[str]) -> str:
    """Remove acentos e cedilhas, converte para minúsculas."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.create_function("unaccent", 1, _normalize)
    return conn


def _db_populate_norms():
    """Popula title_norm/artist_norm e sincroniza FTS5. Roda em background."""
    try:
        conn = _db_connect()
        # Preenche colunas norm onde ainda estão vazias
        rows = conn.execute(
            "SELECT id, title, artist, clean_filename FROM songs WHERE title_norm IS NULL OR artist_norm IS NULL"
        ).fetchall()
        if rows:
            log.info(f"Populando norm columns para {len(rows)} músicas...")
            batch = [
                (_normalize(r["title"] or r["clean_filename"] or ""),
                 _normalize(r["artist"] or ""),
                 r["id"])
                for r in rows
            ]
            conn.executemany(
                "UPDATE songs SET title_norm=?, artist_norm=? WHERE id=?", batch
            )
            conn.commit()
            log.info("Norm columns OK")
        # Sincroniza FTS: insere IDs que ainda não estão na tabela FTS
        fts_count = conn.execute("SELECT COUNT(*) FROM songs_fts").fetchone()[0]
        songs_count = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        if fts_count < songs_count:
            log.info(f"Reconstruindo FTS5 ({songs_count} músicas)...")
            conn.execute("DELETE FROM songs_fts")
            rows = conn.execute(
                "SELECT id, title_norm, artist_norm FROM songs WHERE title_norm IS NOT NULL"
            ).fetchall()
            conn.executemany(
                "INSERT INTO songs_fts(rowid, title_norm, artist_norm) VALUES (?, ?, ?)",
                [(r["id"], r["title_norm"] or "", r["artist_norm"] or "") for r in rows],
            )
            conn.commit()
            log.info(f"FTS5 OK — {len(rows)} entradas")
        conn.close()
    except Exception as e:
        log.error(f"_db_populate_norms error: {e}")


def db_init():
    """Cria/migra todas as tabelas do banco de dados."""
    try:
        conn = _db_connect()
        # Tabela principal de músicas (criada aqui para permitir instalação do zero)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT,
                clean_filename TEXT,
                filepath TEXT NOT NULL,
                artist TEXT DEFAULT '',
                title TEXT DEFAULT '',
                file_type TEXT DEFAULT '',
                musical_key TEXT DEFAULT '',
                duration_seconds REAL DEFAULT 0,
                has_vocals INTEGER DEFAULT 1,
                title_norm TEXT,
                artist_norm TEXT,
                file_size_kb REAL,
                publisher TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_musical_key ON songs(musical_key)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS play_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                filepath TEXT DEFAULT '',
                user TEXT DEFAULT '',
                semitones INTEGER DEFAULT 0,
                played_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # Migração: adiciona coluna semitones se não existir
        try:
            conn.execute("ALTER TABLE play_history ADD COLUMN semitones INTEGER DEFAULT 0")
        except Exception:
            pass  # coluna já existe
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_filepath ON play_history(filepath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_played_at ON play_history(played_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_user ON play_history(user)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transposed_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filepath TEXT NOT NULL,
                semitones INTEGER NOT NULL,
                transposed_filepath TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(original_filepath, semitones)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ts_lookup ON transposed_songs(original_filepath, semitones)"
        )
        # Migração: colunas normalizadas para busca rápida
        for col in ("title_norm", "artist_norm"):
            try:
                conn.execute(f"ALTER TABLE songs ADD COLUMN {col} TEXT")
            except Exception:
                pass  # já existe
        # Migração: tamanho do arquivo em KB
        try:
            conn.execute("ALTER TABLE songs ADD COLUMN file_size_kb REAL")
        except Exception:
            pass  # já existe
        # Migração: editora da música
        try:
            conn.execute("ALTER TABLE songs ADD COLUMN publisher TEXT")
        except Exception:
            pass  # já existe
        conn.execute("CREATE INDEX IF NOT EXISTS idx_title_norm ON songs(title_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artist_norm ON songs(artist_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_filepath ON songs(filepath)")
        # FTS5 trigram para busca ultra-rápida
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5(
                title_norm, artist_norm,
                tokenize='trigram'
            )
        """)
        # Playlists criadas pelo usuário
        conn.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'private',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS playlist_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                filepath TEXT NOT NULL,
                semitones INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                added_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(playlist_id, filepath),
                FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ps_playlist ON playlist_songs(playlist_id, position)"
        )
        conn.commit()
        conn.close()
        log.info("DB tables OK")
    except Exception as e:
        log.error(f"db_init error: {e}")


def db_search(query: str, limit: int = 50, search_type: str = "title", vocal_only: bool = False) -> list[dict]:
    q_norm = _normalize(query)
    vocal_filter = "AND s.has_vocals = 1" if vocal_only else ""
    try:
        conn = _db_connect()
        # Tenta FTS5 (muito mais rápido que LIKE com função Python)
        try:
            if search_type == "artist":
                fts_where = f"fts.artist_norm MATCH ?"
            elif search_type == "both":
                fts_where = f"(fts.title_norm MATCH ? OR fts.artist_norm MATCH ?)"
            else:
                fts_where = f"fts.title_norm MATCH ?"
            fts_match = f'"{q_norm}"' if len(q_norm) >= 3 else f'"{q_norm}"*'
            fts_params_count = 2 if search_type == "both" else 1
            sql = f"""
                SELECT s.id, s.artist, s.title, s.filepath, s.clean_filename,
                       s.musical_key, s.duration_seconds, s.file_type,
                       s.has_vocals, s.publisher,
                       ph.play_count, ph.last_played
                FROM songs_fts fts
                JOIN songs s ON fts.rowid = s.id
                LEFT JOIN (
                    SELECT filepath, COUNT(*) as play_count, MAX(played_at) as last_played
                    FROM play_history WHERE filepath != ''
                    GROUP BY filepath
                ) ph ON s.filepath = ph.filepath
                WHERE {fts_where} {vocal_filter}
                ORDER BY fts.rank
                LIMIT ?
            """
            params = tuple([fts_match] * fts_params_count) + (limit,)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            pass  # FTS não disponível/ainda populando, usa LIKE
        # Fallback: LIKE nas colunas pre-normalizadas
        q_like = f"%{q_norm}%"
        if search_type == "artist":
            sql = f"""
                SELECT s.id, s.artist, s.title, s.filepath, s.clean_filename,
                       s.musical_key, s.duration_seconds, s.file_type,
                       s.has_vocals, s.publisher,
                       ph.play_count, ph.last_played
                FROM songs s
                LEFT JOIN (
                    SELECT filepath, COUNT(*) as play_count, MAX(played_at) as last_played
                    FROM play_history WHERE filepath != ''
                    GROUP BY filepath
                ) ph ON s.filepath = ph.filepath
                WHERE COALESCE(s.artist_norm, unaccent(s.artist)) LIKE ? {vocal_filter}
                ORDER BY s.artist_norm, s.title_norm
                LIMIT ?
            """
            params = (q_like, limit)
        elif search_type == "both":
            sql = f"""
                SELECT s.id, s.artist, s.title, s.filepath, s.clean_filename,
                       s.musical_key, s.duration_seconds, s.file_type,
                       s.has_vocals, s.publisher,
                       ph.play_count, ph.last_played
                FROM songs s
                LEFT JOIN (
                    SELECT filepath, COUNT(*) as play_count, MAX(played_at) as last_played
                    FROM play_history WHERE filepath != ''
                    GROUP BY filepath
                ) ph ON s.filepath = ph.filepath
                WHERE (COALESCE(s.title_norm, unaccent(s.title)) LIKE ?
                    OR COALESCE(s.artist_norm, unaccent(s.artist)) LIKE ?) {vocal_filter}
                ORDER BY
                    CASE WHEN COALESCE(s.title_norm, unaccent(s.title)) LIKE ? THEN 0 ELSE 1 END,
                    s.title_norm
                LIMIT ?
            """
            params = (q_like, q_like, q_like, limit)
        else:
            sql = f"""
                SELECT s.id, s.artist, s.title, s.filepath, s.clean_filename,
                       s.musical_key, s.duration_seconds, s.file_type,
                       s.has_vocals, s.publisher,
                       ph.play_count, ph.last_played
                FROM songs s
                LEFT JOIN (
                    SELECT filepath, COUNT(*) as play_count, MAX(played_at) as last_played
                    FROM play_history WHERE filepath != ''
                    GROUP BY filepath
                ) ph ON s.filepath = ph.filepath
                WHERE COALESCE(s.title_norm, unaccent(s.title)) LIKE ? {vocal_filter}
                ORDER BY
                    CASE WHEN COALESCE(s.title_norm, unaccent(s.title)) LIKE ? THEN 0 ELSE 1 END,
                    s.title_norm
                LIMIT ?
            """
            params = (q_like, q_like, limit)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"db_search error: {e}")
        return []


def db_browse(
    letter: Optional[str] = None,
    sort: str = "title",
    page: int = 1,
    per_page: int = 100,
) -> dict:
    where_clauses: list[str] = []
    params: list = []

    if letter:
        if letter == "numeric":
            where_clauses.append(
                "(substr(s.clean_filename, 1, 1) >= '0' AND substr(s.clean_filename, 1, 1) <= '9')"
            )
        else:
            where_clauses.append("COALESCE(substr(s.title_norm, 1, 1), unaccent(substr(s.clean_filename, 1, 1))) = ?")
            params.append(_normalize(letter))

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    if sort == "artist":
        order_sql = "ORDER BY COALESCE(s.artist_norm, unaccent(s.artist)), COALESCE(s.title_norm, unaccent(s.clean_filename))"
    else:
        order_sql = "ORDER BY COALESCE(s.title_norm, unaccent(s.clean_filename))"

    try:
        conn = _db_connect()
        total = conn.execute(
            f"SELECT COUNT(*) FROM songs s {where_sql}", params
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"""
            SELECT s.id, s.artist, s.title, s.filepath, s.clean_filename,
                   s.musical_key, s.duration_seconds, s.file_type,
                   s.has_vocals, s.publisher,
                   ph.play_count, ph.last_played
            FROM songs s
            LEFT JOIN (
                SELECT filepath, COUNT(*) as play_count, MAX(played_at) as last_played
                FROM play_history WHERE filepath != ''
                GROUP BY filepath
            ) ph ON s.filepath = ph.filepath
            {where_sql} {order_sql}
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()
        conn.close()
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "songs": [dict(r) for r in rows],
        }
    except Exception as e:
        log.error(f"db_browse error: {e}")
        return {"total": 0, "page": page, "per_page": per_page, "songs": []}


EDITABLE_FIELDS = {"artist", "title", "musical_key", "has_vocals"}


def db_suggest_artists(prefix: str, limit: int = 10) -> list[str]:
    q_norm = _normalize(prefix)
    q_like = f"%{q_norm}%"
    try:
        conn = _db_connect()
        rows = conn.execute(
            """
            SELECT DISTINCT artist FROM songs
            WHERE artist IS NOT NULL AND artist != ''
              AND COALESCE(artist_norm, unaccent(artist)) LIKE ?
            ORDER BY
                CASE WHEN COALESCE(artist_norm, unaccent(artist)) LIKE ? THEN 0 ELSE 1 END,
                COALESCE(artist_norm, unaccent(artist))
            LIMIT ?
            """,
            (q_like, f"{q_norm}%", limit),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        log.error(f"db_suggest_artists error: {e}")
        return []


def db_suggest_users(prefix: str, limit: int = 8) -> list[str]:
    """Retorna usuários do play_history que correspondem ao prefixo."""
    q_norm = _normalize(prefix)
    q_like = f"%{q_norm}%"
    try:
        conn = _db_connect()
        rows = conn.execute(
            """
            SELECT DISTINCT user FROM play_history
            WHERE user IS NOT NULL AND user != ''
              AND unaccent(user) LIKE ?
            ORDER BY
                CASE WHEN unaccent(user) LIKE ? THEN 0 ELSE 1 END,
                user
            LIMIT ?
            """,
            (q_like, f"{q_norm}%", limit),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        log.error(f"db_suggest_users error: {e}")
        return []


def db_delete_song(song_id: int) -> dict:
    """Remove a música do banco e move o arquivo para a pasta 'excluido'. Retorna {ok, filepath, error}."""
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT filepath, title, artist FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "Música não encontrada no banco"}

        filepath = row["filepath"]
        title = row["title"] or row["artist"] or os.path.basename(filepath)

        # Move o arquivo para a subpasta "excluido" dentro do mesmo diretório
        if filepath and os.path.exists(filepath):
            parent_dir = os.path.dirname(filepath)
            excluido_dir = os.path.join(parent_dir, "excluido")
            os.makedirs(excluido_dir, exist_ok=True)
            dest = os.path.join(excluido_dir, os.path.basename(filepath))
            # Se já existe um arquivo com o mesmo nome, adiciona sufixo
            if os.path.exists(dest):
                base, ext = os.path.splitext(os.path.basename(filepath))
                dest = os.path.join(excluido_dir, f"{base}_{song_id}{ext}")
            shutil.move(filepath, dest)
            log.info(f"Arquivo movido para excluido: {dest}")
        elif filepath:
            log.warning(f"Arquivo não encontrado no disco: {filepath}")

        conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
        conn.close()
        log.info(f"Música excluída do banco: [{song_id}] {title}")
        return {"ok": True, "filepath": filepath, "title": title}
    except Exception as e:
        log.error(f"db_delete_song error: {e}")
        return {"ok": False, "error": str(e)}


def db_update_song(song_id: int, fields: dict) -> Optional[dict]:
    safe = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not safe:
        return None
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    params = list(safe.values()) + [song_id]
    try:
        conn = _db_connect()
        conn.execute(f"UPDATE songs SET {set_clause} WHERE id = ?", params)
        conn.commit()
        row = conn.execute(
            "SELECT id, artist, title, filepath, clean_filename, musical_key,"
            " duration_seconds, file_type, has_vocals FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.error(f"db_update_song error: {e}")
        return None


def db_record_play(title: str, filepath: str = "", user: str = "", semitones: int = 0) -> int:
    """Registra uma música tocada. Retorna o ID do registro criado."""
    try:
        conn = _db_connect()
        cur = conn.execute(
            "INSERT INTO play_history (title, filepath, user, semitones) VALUES (?, ?, ?, ?)",
            (title, filepath, user, semitones),
        )
        play_id = cur.lastrowid
        conn.commit()
        conn.close()
        log.info(f"Play registrado: {title} (id={play_id})")
        return play_id
    except Exception as e:
        log.error(f"db_record_play error: {e}")
        return 0


def db_update_play_semitones(play_id: int, semitones: int) -> None:
    """Atualiza os semitones de um registro de play_history."""
    if not play_id:
        return
    try:
        conn = _db_connect()
        conn.execute("UPDATE play_history SET semitones = ? WHERE id = ?", (semitones, play_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"db_update_play_semitones error: {e}")


def db_get_user_last_semitone(filepath: str, user: str) -> int:
    """Retorna o último semitom usado pelo usuário nessa música, ou 0."""
    if not filepath or not user:
        return 0
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT semitones FROM play_history WHERE filepath = ? AND user = ? ORDER BY played_at DESC LIMIT 1",
            (filepath, user),
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        log.error(f"db_get_user_last_semitone error: {e}")
        return 0


def _resolve_original_filepath_sql():
    """SQL fragment that resolves transposed filepaths back to the original song.

    Handles up to 2 levels of transposition chain:
    original.zip → original_Tom-2.zip → original_Tom-2_Tom-3.zip
    """
    return """
        LEFT JOIN transposed_songs ts1 ON ts1.transposed_filepath = ph.filepath
        LEFT JOIN transposed_songs ts2 ON ts2.transposed_filepath = ts1.original_filepath
    """


def _resolved_filepath_expr():
    """SQL expression for the ultimate original filepath."""
    return "COALESCE(ts2.original_filepath, ts1.original_filepath, ph.filepath)"


def db_get_history(page: int = 1, per_page: int = 50, user_filter: Optional[str] = None) -> dict:
    offset = (page - 1) * per_page
    user_where = "AND ph.user = ?" if user_filter else ""
    user_params: list = [user_filter] if user_filter else []
    resolved = _resolved_filepath_expr()
    resolve_joins = _resolve_original_filepath_sql()
    try:
        conn = _db_connect()
        # Deduplica por música original: agrupa transposições da mesma música,
        # mostra a entrada mais recente, com contagem total
        total = conn.execute(
            f"""SELECT COUNT(*) FROM (
                SELECT {resolved} as resolved_fp
                FROM play_history ph
                {resolve_joins}
                WHERE ph.filepath != '' {user_where}
                GROUP BY resolved_fp
            )""",
            user_params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT ph.id, ph.title, ph.filepath, ph.user, ph.played_at, ph.semitones,
                   s.id as song_id, s.title as song_title, s.artist,
                   s.clean_filename, s.musical_key, s.has_vocals, s.publisher,
                   pc.play_count
            FROM play_history ph
            INNER JOIN (
                SELECT {resolved} as resolved_fp,
                       MAX(ph.id) as max_id, COUNT(*) as play_count
                FROM play_history ph
                {resolve_joins}
                WHERE ph.filepath != '' {user_where}
                GROUP BY resolved_fp
            ) pc ON ph.id = pc.max_id
            {resolve_joins}
            LEFT JOIN songs s ON s.filepath = {resolved}
            ORDER BY ph.played_at DESC
            LIMIT ? OFFSET ?
            """,
            user_params + [per_page, offset],
        ).fetchall()
        conn.close()
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [dict(r) for r in rows],
        }
    except Exception as e:
        log.error(f"db_get_history error: {e}")
        return {"total": 0, "page": page, "per_page": per_page, "items": []}


# ---------------------------------------------------------------------------
# Playlists — CRUD
# ---------------------------------------------------------------------------

def db_get_playlists(user: str) -> list[dict]:
    """Retorna playlists visíveis para o usuário: todas públicas + privadas do próprio user."""
    try:
        conn = _db_connect()
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.owner, p.visibility, p.created_at,
                   COUNT(ps.id) as song_count
            FROM playlists p
            LEFT JOIN playlist_songs ps ON ps.playlist_id = p.id
            WHERE p.visibility != 'private' OR p.owner = ?
            GROUP BY p.id
            ORDER BY p.owner = ? DESC, p.name
            """,
            (user, user),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"db_get_playlists error: {e}")
        return []


def db_create_playlist(name: str, owner: str, visibility: str) -> Optional[dict]:
    """Cria uma playlist. Retorna o dict da playlist criada."""
    valid = {"private", "public_play", "public_edit"}
    if visibility not in valid:
        visibility = "private"
    try:
        conn = _db_connect()
        cur = conn.execute(
            "INSERT INTO playlists (name, owner, visibility) VALUES (?, ?, ?)",
            (name, owner, visibility),
        )
        pl_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT id, name, owner, visibility, created_at FROM playlists WHERE id = ?", (pl_id,)).fetchone()
        conn.close()
        return {**dict(row), "song_count": 0} if row else None
    except Exception as e:
        log.error(f"db_create_playlist error: {e}")
        return None


def db_get_playlist_songs(playlist_id: int) -> list[dict]:
    """Retorna as músicas de uma playlist ordenadas por posição."""
    try:
        conn = _db_connect()
        rows = conn.execute(
            """
            SELECT ps.filepath, ps.semitones, ps.position,
                   s.title AS title,
                   COALESCE(s.artist, '') AS artist,
                   COALESCE(s.musical_key, '') AS musical_key,
                   s.clean_filename AS clean_filename,
                   COALESCE(s.has_vocals, 0) AS has_vocals,
                   COALESCE(s.publisher, '') AS publisher,
                   s.id AS song_id
            FROM playlist_songs ps
            LEFT JOIN songs s ON s.filepath = ps.filepath
            WHERE ps.playlist_id = ?
            ORDER BY ps.position
            """,
            (playlist_id,),
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            # Fallback para músicas não indexadas: extrai nome do filepath
            if not d.get("clean_filename") and d.get("filepath"):
                stem = os.path.splitext(os.path.basename(d["filepath"]))[0]
                d["clean_filename"] = stem
            result.append(d)
        return result
    except Exception as e:
        log.error(f"db_get_playlist_songs error: {e}")
        return []


def db_add_song_to_playlist(playlist_id: int, filepath: str, semitones: int) -> bool:
    """Adiciona música ao final da playlist. Ignora se já estiver na playlist."""
    try:
        conn = _db_connect()
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO playlist_songs (playlist_id, filepath, semitones, position) VALUES (?, ?, ?, ?)",
            (playlist_id, filepath, semitones, max_pos + 1),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"db_add_song_to_playlist error: {e}")
        return False


def db_remove_song_from_playlist(playlist_id: int, filepath: str) -> bool:
    """Remove uma música da playlist."""
    try:
        conn = _db_connect()
        conn.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND filepath = ?",
            (playlist_id, filepath),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"db_remove_song_from_playlist error: {e}")
        return False


def db_reorder_playlist(playlist_id: int, ordered_filepaths: list) -> bool:
    """Reordena as músicas da playlist. ordered_filepaths é a lista na nova ordem."""
    try:
        conn = _db_connect()
        for i, fp in enumerate(ordered_filepaths):
            conn.execute(
                "UPDATE playlist_songs SET position = ? WHERE playlist_id = ? AND filepath = ?",
                (i, playlist_id, fp),
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"db_reorder_playlist error: {e}")
        return False


def db_update_playlist(playlist_id: int, name: str, visibility: str, user: str) -> Optional[dict]:
    """Atualiza nome e visibilidade de uma playlist. Apenas o owner pode atualizar."""
    valid = {"private", "public_play", "public_edit"}
    if visibility not in valid:
        return None
    try:
        conn = _db_connect()
        # Valida que o usuário é o owner
        owner_row = conn.execute("SELECT owner FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if not owner_row or owner_row["owner"] != user:
            conn.close()
            return None
        conn.execute(
            "UPDATE playlists SET name = ?, visibility = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (name, visibility, playlist_id),
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = ?", (playlist_id,)).fetchone()[0]
        row = conn.execute("SELECT id, name, owner, visibility, created_at FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        conn.close()
        return {**dict(row), "song_count": count} if row else None
    except Exception as e:
        log.error(f"db_update_playlist error: {e}")
        return None


def db_delete_playlist(playlist_id: int, user: str) -> bool:
    """Exclui uma playlist. Apenas o owner pode excluir."""
    try:
        conn = _db_connect()
        owner_row = conn.execute("SELECT owner FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if not owner_row or owner_row["owner"] != user:
            conn.close()
            return False
        conn.execute("DELETE FROM playlist_songs WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"db_delete_playlist error: {e}")
        return False


def db_update_playlist_song_semitone(playlist_id: int, filepath: str, semitones: int) -> bool:
    """Atualiza o semitom de uma música em uma playlist."""
    try:
        conn = _db_connect()
        conn.execute(
            "UPDATE playlist_songs SET semitones = ? WHERE playlist_id = ? AND filepath = ?",
            (semitones, playlist_id, filepath),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"db_update_playlist_song_semitone error: {e}")
        return False


# ---------------------------------------------------------------------------
# Inserção de músicas baixadas no banco
# ---------------------------------------------------------------------------

def db_insert_downloaded_song(filepath: str, title_hint: str = "") -> Optional[int]:
    """Insere música baixada no banco de dados. Retorna o ID criado ou None."""
    if not filepath or not os.path.exists(filepath):
        return None
    try:
        import subprocess as _sp
        filename = os.path.basename(filepath)
        stem, raw_ext = os.path.splitext(filename)
        ext = raw_ext.lstrip(".").lower()

        # Remove sufixo ---ID do yt-dlp (ex: Título da Música---dQw4w9WgXcQ)
        clean_stem = re.sub(r"---[A-Za-z0-9_-]{6,20}$", "", stem).strip()

        # Tenta extrair artista - título do hint do YouTube ("Artista - Título")
        artist = ""
        title = title_hint or clean_stem
        if title_hint:
            parts = title_hint.split(" - ", 1)
            if len(parts) == 2 and 2 <= len(parts[0]) <= 60:
                artist = parts[0].strip()
                title = parts[1].strip()

        # Duração via ffprobe
        duration = 0.0
        try:
            r = _sp.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                capture_output=True, text=True, timeout=15,
            )
            duration = float(r.stdout.strip() or "0")
        except Exception:
            pass

        conn = _db_connect()
        # Não duplica se filepath ou original_filename já existe
        existing = conn.execute(
            "SELECT id FROM songs WHERE filepath = ? OR original_filename = ? LIMIT 1",
            (filepath, filename),
        ).fetchone()
        if existing:
            conn.close()
            return existing["id"]

        title_norm = _normalize(title)
        artist_norm = _normalize(artist)
        cur = conn.execute(
            "INSERT INTO songs (original_filename, clean_filename, filepath, artist, title, file_type, duration_seconds, has_vocals, title_norm, artist_norm)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (filename, clean_stem, filepath, artist, title, ext, duration, title_norm, artist_norm),
        )
        song_id = cur.lastrowid
        # Atualiza FTS
        conn.execute(
            "INSERT INTO songs_fts(rowid, title_norm, artist_norm) VALUES (?, ?, ?)",
            (song_id, title_norm, artist_norm),
        )
        conn.commit()
        conn.close()
        log.info(f"Música inserida no banco: [{song_id}] {artist} - {title}")
        return song_id
    except Exception as e:
        log.error(f"db_insert_downloaded_song error: {e}")
        return None


# ---------------------------------------------------------------------------
# Transposed songs — cache no DB
# ---------------------------------------------------------------------------

def db_get_song_by_filepath(filepath: str) -> Optional[dict]:
    """Retorna {title, artist} da música pelo filepath, ou None."""
    if not filepath:
        return None
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT title, artist FROM songs WHERE filepath = ? LIMIT 1", (filepath,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.error(f"db_get_song_by_filepath error: {e}")
        return None


def db_get_random_songs(amount: int, exclude_paths: set = None) -> list:
    """Retorna músicas aleatórias do DB, excluindo as já na fila."""
    try:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT id, filepath, artist, title, clean_filename FROM songs ORDER BY RANDOM() LIMIT ?",
            (amount * 4,)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            if exclude_paths and r["filepath"] in exclude_paths:
                continue
            result.append(dict(r))
            if len(result) >= amount:
                break
        return result
    except Exception as e:
        log.error(f"db_get_random_songs error: {e}")
        return []


def db_get_original_for_transposed_file(transposed_filepath: str) -> Optional[str]:
    """Retorna o filepath original de um arquivo transposto, ou None se não encontrado."""
    if not transposed_filepath:
        return None
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT original_filepath FROM transposed_songs WHERE transposed_filepath = ? LIMIT 1",
            (transposed_filepath,)
        ).fetchone()
        conn.close()
        return row["original_filepath"] if row else None
    except Exception as e:
        log.error(f"db_get_original_for_transposed_file error: {e}")
        return None


def db_get_semitone_for_transposed_file(transposed_filepath: str) -> int:
    """Retorna o semitom de um arquivo transposto (ex: song_Tom+3.zip), ou 0 se não for transposto."""
    if not transposed_filepath:
        return 0
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT semitones FROM transposed_songs WHERE transposed_filepath = ? LIMIT 1",
            (transposed_filepath,)
        ).fetchone()
        conn.close()
        return row["semitones"] if row else 0
    except Exception as e:
        log.error(f"db_get_semitone_for_transposed_file error: {e}")
        return 0


def db_get_transposed(original_filepath: str, semitones: int) -> Optional[str]:
    """Retorna o filepath do arquivo transposto em cache, ou None."""
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT transposed_filepath FROM transposed_songs WHERE original_filepath = ? AND semitones = ?",
            (original_filepath, semitones),
        ).fetchone()
        conn.close()
        if row:
            path = row[0]
            return path if os.path.exists(path) else None
        return None
    except Exception as e:
        log.error(f"db_get_transposed error: {e}")
        return None


def db_save_transposed(original_filepath: str, semitones: int, transposed_filepath: str) -> None:
    """Salva o caminho da versão transposta no cache."""
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO transposed_songs (original_filepath, semitones, transposed_filepath) VALUES (?, ?, ?)",
            (original_filepath, semitones, transposed_filepath),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"db_save_transposed error: {e}")


def get_transposed_path(original_filepath: str, semitones: int) -> str:
    """Retorna o caminho onde o arquivo transposto deve ser salvo."""
    p = Path(original_filepath)
    sign = f"+{semitones}" if semitones >= 0 else str(semitones)
    return str(p.parent / f"{p.stem}_Tom{sign}{p.suffix}")


# ---------------------------------------------------------------------------
# Processamento de tom permanente com ffmpeg + rubberband
# ---------------------------------------------------------------------------

FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):([\d.]+)")


async def get_audio_duration(filepath: str) -> float:
    """Retorna duração em segundos via ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return float(stdout.decode().strip() or "0")
    except Exception:
        return 0.0


async def _run_ffmpeg_with_progress(cmd: list, duration: float, job_id: str) -> bool:
    """Executa ffmpeg, emite progresso via WS broadcast. Retorna True se sucesso."""
    log.info(f"[{job_id}] ffmpeg: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        last_pct = -1
        buf = b""
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            # ffmpeg usa \r para sobrescrever a linha de progresso (não \n)
            # então dividimos por ambos para capturar atualizações em tempo real
            parts = buf.replace(b"\r", b"\n").split(b"\n")
            buf = parts[-1]  # guarda fragmento incompleto para o próximo chunk
            for raw_line in parts[:-1]:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                m = FFMPEG_TIME_RE.search(line)
                if m and duration > 0:
                    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    current = h * 3600 + mn * 60 + s
                    pct = min(99, int(current / duration * 100))
                    if pct != last_pct:
                        last_pct = pct
                        await manager.broadcast({
                            "type": "transpose_progress",
                            "payload": {"job_id": job_id, "percent": pct},
                        })
        await proc.wait()
        return proc.returncode == 0
    except Exception as e:
        log.error(f"[{job_id}] ffmpeg error: {e}")
        return False


async def _process_zip_transpose(
    original_filepath: str, semitones: int, output_filepath: str, job_id: str
) -> bool:
    """Transpõe o áudio dentro de um ZIP CDG e salva em output_filepath."""
    pitch_factor = 2 ** (semitones / 12)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            with zipfile.ZipFile(original_filepath) as zf:
                zf.extractall(tmpdir)

            audio_exts = {".mp3", ".m4a", ".ogg", ".wav", ".flac"}
            audio_files = [
                f for f in tmppath.iterdir()
                if f.is_file() and f.suffix.lower() in audio_exts
            ]
            if not audio_files:
                log.error(f"[{job_id}] Nenhum arquivo de áudio no ZIP: {original_filepath}")
                return False

            audio_file = audio_files[0]
            out_audio = tmppath / f"_transposed{audio_file.suffix}"
            duration = await get_audio_duration(str(audio_file))

            cmd = [
                "ffmpeg", "-y", "-i", str(audio_file),
                "-af", f"rubberband=pitch={pitch_factor:.6f}",
                "-c:a", "libmp3lame", "-q:a", "2",
                str(out_audio),
            ]
            ok = await _run_ffmpeg_with_progress(cmd, duration, job_id)
            if not ok:
                return False

            audio_file.unlink()
            out_audio.rename(audio_file)

            Path(output_filepath).parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_filepath, "w", zipfile.ZIP_STORED) as zout:
                for f in tmppath.iterdir():
                    if f.is_file():
                        zout.write(f, f.name)
            return True
    except Exception as e:
        log.error(f"[{job_id}] _process_zip_transpose error: {e}")
        return False


async def _process_mp4_transpose(
    original_filepath: str, semitones: int, output_filepath: str, job_id: str
) -> bool:
    """Transpõe um arquivo de vídeo/áudio com ffmpeg + rubberband."""
    pitch_factor = 2 ** (semitones / 12)
    duration = await get_audio_duration(original_filepath)
    Path(output_filepath).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", original_filepath,
        "-c:v", "copy",
        "-af", f"rubberband=pitch={pitch_factor:.6f}",
        "-c:a", "aac", "-b:a", "192k",
        output_filepath,
    ]
    return await _run_ffmpeg_with_progress(cmd, duration, job_id)


async def process_song_transpose(
    original_filepath: str, semitones: int, job_id: str
) -> tuple[bool, str]:
    """Processa e salva arquivo com tom alterado. Retorna (ok, output_path)."""
    output_path = get_transposed_path(original_filepath, semitones)
    ext = Path(original_filepath).suffix.lower()

    if ext == ".zip":
        ok = await _process_zip_transpose(original_filepath, semitones, output_path, job_id)
    elif ext in (".mp4", ".mkv", ".webm", ".avi"):
        ok = await _process_mp4_transpose(original_filepath, semitones, output_path, job_id)
    else:
        log.warning(f"[{job_id}] Formato não suportado: {ext}")
        return False, ""

    if ok:
        await asyncio.to_thread(db_save_transposed, original_filepath, semitones, output_path)
        log.info(f"[{job_id}] Transposição salva: {output_path}")
    return ok, output_path


async def _restart_with_transposed(transposed_filepath: str, user: str, pk_url: str):
    """Enfileira o arquivo transposto na frente e pula a música atual."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{pk_url}/get_queue")
            queue = r.json() if r.status_code == 200 else []
        except Exception:
            queue = []

        await client.get(f"{pk_url}/enqueue", params={"song": transposed_filepath, "user": user})
        await asyncio.sleep(0.2)

        # Move para o início da fila
        n_moves = len(queue) + 1
        for _ in range(n_moves):
            try:
                await client.get(
                    f"{pk_url}/queue/edit",
                    params={"action": "up", "song": transposed_filepath},
                )
            except Exception:
                break

        await asyncio.sleep(0.2)
        await client.get(f"{pk_url}/skip")


async def _poll_vlc_transpose_status(semitones: int, pk_url: str):
    """Aguarda o VLC terminar de transposir e notifica via WS."""
    if semitones == 0:
        # Sem ffmpeg — VLC apenas reinicia o arquivo original
        await asyncio.sleep(0.8)
        await tracker.poll_nowplaying()
        await manager.broadcast({"type": "vlc_transpose_done", "payload": {"semitones": 0}})
        return
    # Aguarda is_transposing virar True (ffmpeg iniciou)
    await asyncio.sleep(1.0)
    deadline = asyncio.get_event_loop().time() + 90
    try:
        async with httpx.AsyncClient(timeout=5.0) as tc:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await tc.get(f"{pk_url}/transpose/status")
                    if not r.json().get("transposing", True):
                        break
                except Exception:
                    break
                await asyncio.sleep(0.5)
    except Exception:
        pass
    await asyncio.sleep(0.3)
    await tracker.poll_nowplaying()
    await manager.broadcast({"type": "vlc_transpose_done", "payload": {"semitones": semitones}})


async def _process_and_notify(
    filepath: str,
    semitones: int,
    auto_restart: bool,
    user: str,
    pk_url: str,
    job_id: str,
):
    """Tarefa de fundo: processa transposição e notifica todos os clientes."""
    try:
        ok, transposed_path = await process_song_transpose(filepath, semitones, job_id)
        tracker._current_transpose_job_id = ""
        if ok:
            # Play the transposed file at the current position
            if transposed_path:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as hclient:
                        pos_r = await hclient.get(f"{pk_url}/playback_pos")
                        pos_data = pos_r.json()
                        seek_to = pos_data.get("position", 0.0)
                        await hclient.get(
                            f"{pk_url}/play_transposed",
                            params={
                                "filepath": transposed_path,
                                "original": filepath,
                                "seek_to": seek_to,
                                "semitones": semitones,
                            },
                        )
                except Exception as e:
                    log.warning(f"[{job_id}] play_transposed after processing failed: {e}")
                await asyncio.sleep(0.5)
                await tracker.poll_nowplaying()
            await manager.broadcast({
                "type": "transpose_done",
                "payload": {
                    "job_id": job_id,
                    "filepath": transposed_path,
                    "semitones": semitones,
                    "auto_restart": True,
                },
            })
        else:
            await manager.broadcast({
                "type": "transpose_error",
                "payload": {"job_id": job_id, "error": "Falha no processamento ffmpeg"},
            })
    except Exception as e:
        tracker._current_transpose_job_id = ""
        log.error(f"[{job_id}] _process_and_notify error: {e}")
        await manager.broadcast({
            "type": "transpose_error",
            "payload": {"job_id": job_id, "error": str(e)},
        })


# ---------------------------------------------------------------------------
# YouTube search — async subprocess yt-dlp
# ---------------------------------------------------------------------------

async def yt_search(query: str, limit: int = 10) -> list[dict]:
    yt_query = f'ytsearch{limit}:{query} karaoke'
    cmd = [YTDLP_PATH, "-j", "--no-playlist", "--flat-playlist", yt_query]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        results = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
                if "title" not in j:
                    continue
                vid_id = j.get("id", "")
                vid_url = j.get("url") or j.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
                results.append({
                    "title": j["title"],
                    "url": vid_url,
                    "id": vid_id,
                    "thumbnail": f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                    "duration": j.get("duration"),
                })
            except json.JSONDecodeError:
                continue
        return results
    except asyncio.TimeoutError:
        log.error("YouTube search timeout")
        return []
    except Exception as e:
        log.error(f"yt_search error: {e}")
        return []


# ---------------------------------------------------------------------------
# WebSocket command handler
# ---------------------------------------------------------------------------

async def handle_command(ws: WebSocket, msg: dict, pk_url: str = None):
    cmd = msg.get("cmd", "")
    payload = msg.get("payload", {})
    if pk_url is None:
        pk_url = PIKARAOKE_URL

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:

            if cmd == "enqueue":
                filepath = payload.get("filepath", "")
                user = payload.get("user", "Visitante")
                r = await client.get(
                    f"{pk_url}/enqueue",
                    params={"song": filepath, "user": user},
                )
                result = json.loads(r.text)
                # Verifica tom preferido do usuário para essa música
                semitones_hint = 0
                if result.get("success") and filepath and user:
                    semitones_hint = await asyncio.to_thread(db_get_user_last_semitone, filepath, user)
                    if semitones_hint != 0:
                        tracker._pending_transpositions[filepath] = semitones_hint
                result["semitones_hint"] = semitones_hint
                result["filepath"] = filepath
                await manager.send_to(ws, {"type": "enqueue_result", "payload": result})
                await asyncio.sleep(0.3)
                await tracker.poll_queue()

            elif cmd == "skip":
                await client.get(f"{pk_url}/skip")
                await asyncio.sleep(0.5)
                await tracker.poll_nowplaying()
                await tracker.poll_queue()

            elif cmd == "pause":
                await client.get(f"{pk_url}/pause")
                await asyncio.sleep(0.3)
                await tracker.poll_nowplaying()

            elif cmd == "restart":
                await client.get(f"{pk_url}/restart")
                await asyncio.sleep(0.5)
                await tracker.poll_nowplaying()

            elif cmd == "vol_up":
                await client.get(f"{pk_url}/vol_up")

            elif cmd == "vol_down":
                await client.get(f"{pk_url}/vol_down")

            elif cmd == "vol_set":
                percent = int(payload.get("percent", 50))
                vol = await asyncio.to_thread(set_system_volume, percent)
                tracker._system_volume = vol
                await manager.broadcast({"type": "vol_update", "payload": {"percent": vol}})

            elif cmd == "seek":
                t = float(payload.get("t", 0))
                await client.get(f"{pk_url}/seek", params={"t": t})
                await asyncio.sleep(0.2)
                # Busca posição real pós-seek e sincroniza todos os clientes
                try:
                    pos_r = await client.get(f"{pk_url}/playback_pos")
                    pos_data = pos_r.json()
                    tracker._cached_position = pos_data.get("position", t)
                    tracker._cached_duration = pos_data.get("duration", tracker._cached_duration)
                except Exception:
                    tracker._cached_position = t
                await manager.broadcast({
                    "type": "seek_update",
                    "payload": {
                        "position": tracker._cached_position,
                        "duration": tracker._cached_duration,
                    },
                })

            elif cmd == "transpose":
                semitones = int(payload.get("semitones", 0))
                await manager.broadcast({"type": "vlc_transpose_started", "payload": {"semitones": semitones}})
                await client.get(f"{pk_url}/transpose/{semitones}")
                # Salva semitom no histórico e atualiza estado
                if tracker._current_play_id:
                    await asyncio.to_thread(db_update_play_semitones, tracker._current_play_id, semitones)
                tracker._current_semitones = semitones
                await manager.broadcast({"type": "semitone_changed", "payload": {"semitones": semitones}})
                asyncio.create_task(_poll_vlc_transpose_status(semitones, pk_url))

            elif cmd == "queue_edit":
                action = payload.get("action", "")
                song = payload.get("song", "")
                await client.get(
                    f"{pk_url}/queue/edit",
                    params={"action": action, "song": song},
                )
                await asyncio.sleep(0.3)
                await tracker.poll_queue()

            elif cmd == "queue_clear":
                await client.get(
                    f"{pk_url}/queue/edit",
                    params={"action": "clear"},
                )
                await asyncio.sleep(0.3)
                await tracker.poll_queue()
                await tracker.poll_nowplaying()

            elif cmd == "queue_add_random":
                amount = int(payload.get("amount", 3))
                user = payload.get("user", "Randomizer")
                queued_files = {item["file"] for item in tracker._queue}
                songs = await asyncio.to_thread(db_get_random_songs, amount, queued_files)
                for song in songs:
                    await client.get(f"{pk_url}/enqueue", params={"song": song["filepath"], "user": user})
                await asyncio.sleep(0.3)
                await tracker.poll_queue()

            elif cmd == "search_local":
                query = payload.get("q", "")
                search_type = payload.get("type", "title")
                vocal_only = bool(payload.get("vocal_only", False))
                if len(query) >= 2:
                    results = await asyncio.to_thread(db_search, query, 50, search_type, vocal_only)
                    await manager.send_to(ws, {
                        "type": "search_results",
                        "payload": {"q": query, "results": results},
                    })

            elif cmd == "browse":
                letter = payload.get("letter") or None
                sort = payload.get("sort", "alpha")
                page = int(payload.get("page", 1))
                result = await asyncio.to_thread(db_browse, letter, sort, page)
                await manager.send_to(ws, {
                    "type": "browse_results",
                    "payload": result,
                })

            elif cmd == "search_youtube":
                query = payload.get("q", "").strip()
                if query:
                    results = await yt_search(query)
                    await manager.send_to(ws, {
                        "type": "youtube_results",
                        "payload": {"q": query, "results": results},
                    })

            elif cmd == "suggest_artists":
                prefix = payload.get("q", "").strip()
                if len(prefix) >= 1:
                    suggestions = await asyncio.to_thread(db_suggest_artists, prefix)
                    await manager.send_to(ws, {
                        "type": "artist_suggestions",
                        "payload": {"q": prefix, "suggestions": suggestions},
                    })

            elif cmd == "suggest_users":
                prefix = payload.get("q", "").strip()
                if len(prefix) >= 1:
                    suggestions = await asyncio.to_thread(db_suggest_users, prefix)
                    await manager.send_to(ws, {
                        "type": "user_suggestions",
                        "payload": {"q": prefix, "users": suggestions},
                    })

            elif cmd == "song_edit":
                song_id = int(payload.get("id", 0))
                fields = {k: payload[k] for k in ("artist", "title", "musical_key", "has_vocals") if k in payload}
                updated = await asyncio.to_thread(db_update_song, song_id, fields)
                if updated:
                    await manager.send_to(ws, {"type": "song_edit_result", "payload": {"success": True, "song": updated}})
                else:
                    await manager.send_to(ws, {"type": "song_edit_result", "payload": {"success": False}})

            elif cmd == "delete_song":
                song_id = int(payload.get("id", 0))
                result = await asyncio.to_thread(db_delete_song, song_id)
                await manager.send_to(ws, {"type": "delete_song_result", "payload": {"song_id": song_id, **result}})
                if result["ok"]:
                    # Atualiza biblioteca do PiKaraoke
                    try:
                        await client.get(f"{pk_url}/refresh")
                    except Exception:
                        pass

            elif cmd == "history":
                page = int(payload.get("page", 1))
                user_filter = payload.get("user") or None
                result = await asyncio.to_thread(db_get_history, page, 50, user_filter)
                await manager.send_to(ws, {
                    "type": "history_results",
                    "payload": result,
                })

            elif cmd == "download":
                url = payload.get("url", "").strip()
                user = payload.get("user", "Visitante")
                enqueue = bool(payload.get("enqueue", True))
                title = payload.get("title", "")
                if url:
                    await download_manager.start(url, user, enqueue, title)

            elif cmd == "transpose_process":
                semitones = int(payload.get("semitones", 0))
                filepath = payload.get("filepath", "").strip()
                auto_restart = bool(payload.get("auto_restart", False))
                user = payload.get("user", "Visitante")
                job_id = str(uuid.uuid4())[:8]

                # Tenta obter filepath se não fornecido pelo cliente
                if not filepath:
                    filepath = tracker._current_playing_filepath
                if not filepath and tracker._nowplaying:
                    title = tracker._nowplaying.get("now_playing", "")
                    if title:
                        results = await asyncio.to_thread(db_search, title, 1, "title")
                        if results:
                            filepath = results[0].get("filepath", "")

                if not filepath:
                    await manager.send_to(ws, {
                        "type": "transpose_error",
                        "payload": {"job_id": job_id, "error": "Arquivo não encontrado para a música atual"},
                    })
                elif semitones == 0:
                    # Voltar ao tom original — atualiza histórico e estado
                    if tracker._current_play_id:
                        await asyncio.to_thread(db_update_play_semitones, tracker._current_play_id, 0)
                    tracker._current_semitones = 0
                    await manager.broadcast({"type": "semitone_changed", "payload": {"semitones": 0}})
                    # Se a música atual é um arquivo transposto, reinicia com o original
                    restarted = False
                    if auto_restart and filepath:
                        original = await asyncio.to_thread(db_get_original_for_transposed_file, filepath)
                        if original and os.path.exists(original):
                            await _restart_with_transposed(original, user, pk_url)
                            await asyncio.sleep(0.5)
                            await tracker.poll_queue()
                            await tracker.poll_nowplaying()
                            restarted = True
                    await manager.broadcast({
                        "type": "transpose_cached",
                        "payload": {"job_id": job_id, "filepath": filepath, "semitones": 0, "auto_restart": restarted},
                    })
                else:
                    # Salva semitom no histórico imediatamente e atualiza estado
                    if tracker._current_play_id:
                        await asyncio.to_thread(db_update_play_semitones, tracker._current_play_id, semitones)
                    tracker._current_semitones = semitones
                    await manager.broadcast({"type": "semitone_changed", "payload": {"semitones": semitones}})

                    # Verifica cache — se já processado, usa direto (sem reprocessar)
                    cached_path = await asyncio.to_thread(db_get_transposed, filepath, semitones)
                    if cached_path:
                        log.info(f"[{job_id}] Cache hit: {cached_path}")
                        # Cache hit: play cached file at current position (no restart)
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                pos_r = await client.get(f"{pk_url}/playback_pos")
                                pos_data = pos_r.json()
                                seek_to = pos_data.get("position", 0.0)
                                await client.get(
                                    f"{pk_url}/play_transposed",
                                    params={
                                        "filepath": cached_path,
                                        "original": filepath,
                                        "seek_to": seek_to,
                                        "semitones": semitones,
                                    },
                                )
                        except Exception as e:
                            log.warning(f"[{job_id}] play_transposed failed: {e}")
                        await asyncio.sleep(0.5)
                        await tracker.poll_nowplaying()
                        await manager.broadcast({
                            "type": "transpose_cached",
                            "payload": {
                                "job_id": job_id,
                                "filepath": cached_path,
                                "semitones": semitones,
                                "auto_restart": True,
                            },
                        })
                    else:
                        # Sem cache — processa com rubberband (permanente) e salva no banco
                        tracker._current_transpose_job_id = job_id
                        await manager.broadcast({
                            "type": "transpose_started",
                            "payload": {
                                "job_id": job_id,
                                "semitones": semitones,
                                "filepath": filepath,
                            },
                        })
                        asyncio.create_task(
                            _process_and_notify(filepath, semitones, auto_restart, user, pk_url, job_id)
                        )

            elif cmd == "playlist_list":
                user = payload.get("user", "")
                playlists = await asyncio.to_thread(db_get_playlists, user)
                await manager.send_to(ws, {"type": "playlist_list_result", "payload": playlists})

            elif cmd == "playlist_create":
                name = payload.get("name", "").strip()
                owner = payload.get("user", "")
                visibility = payload.get("visibility", "private")
                if name and owner:
                    pl = await asyncio.to_thread(db_create_playlist, name, owner, visibility)
                    await manager.send_to(ws, {"type": "playlist_create_result", "payload": {"success": pl is not None, "playlist": pl}})
                else:
                    await manager.send_to(ws, {"type": "playlist_create_result", "payload": {"success": False}})

            elif cmd == "playlist_songs":
                pl_id = int(payload.get("playlist_id", 0))
                songs = await asyncio.to_thread(db_get_playlist_songs, pl_id)
                await manager.send_to(ws, {"type": "playlist_songs_result", "payload": {"playlist_id": pl_id, "songs": songs}})

            elif cmd == "playlist_add_song":
                pl_id = int(payload.get("playlist_id", 0))
                filepath = payload.get("filepath", "")
                semitones = int(payload.get("semitones", 0))
                success = await asyncio.to_thread(db_add_song_to_playlist, pl_id, filepath, semitones)
                # Busca nome da playlist para o toast
                pl_name = ""
                for pl in (await asyncio.to_thread(db_get_playlists, "")):
                    if pl["id"] == pl_id:
                        pl_name = pl["name"]
                        break
                await manager.send_to(ws, {"type": "playlist_add_result", "payload": {"success": success, "playlist_id": pl_id, "playlist_name": pl_name}})

            elif cmd == "playlist_remove_song":
                pl_id = int(payload.get("playlist_id", 0))
                filepath = payload.get("filepath", "")
                success = await asyncio.to_thread(db_remove_song_from_playlist, pl_id, filepath)
                await manager.send_to(ws, {"type": "playlist_remove_result", "payload": {"success": success, "filepath": filepath}})

            elif cmd == "playlist_reorder":
                pl_id = int(payload.get("playlist_id", 0))
                order = payload.get("order", [])
                success = await asyncio.to_thread(db_reorder_playlist, pl_id, order)
                await manager.send_to(ws, {"type": "playlist_reorder_result", "payload": {"success": success}})

            elif cmd == "playlist_update":
                pl_id = int(payload.get("playlist_id", 0))
                user = payload.get("user", "")
                name = payload.get("name", "")
                visibility = payload.get("visibility", "private")
                updated = await asyncio.to_thread(db_update_playlist, pl_id, name, visibility, user)
                await manager.send_to(ws, {"type": "playlist_update_result", "payload": {"success": updated is not None, "playlist": updated}})

            elif cmd == "playlist_delete":
                pl_id = int(payload.get("playlist_id", 0))
                user = payload.get("user", "")
                success = await asyncio.to_thread(db_delete_playlist, pl_id, user)
                await manager.send_to(ws, {"type": "playlist_delete_result", "payload": {"success": success, "playlist_id": pl_id}})

            elif cmd == "playlist_update_song_semitone":
                pl_id = int(payload.get("playlist_id", 0))
                filepath = payload.get("filepath", "")
                semitones = int(payload.get("semitones", 0))
                success = await asyncio.to_thread(db_update_playlist_song_semitone, pl_id, filepath, semitones)
                await manager.send_to(ws, {"type": "playlist_song_semitone_result", "payload": {"success": success}})

            elif cmd == "playlist_enqueue_all":
                pl_id = payload.get("playlist_id")
                user = payload.get("user", "Visitante")
                if pl_id and pl_id not in ("history_all", "history_user"):
                    songs = await asyncio.to_thread(db_get_playlist_songs, int(pl_id))
                    for song in songs:
                        await client.get(f"{pk_url}/enqueue", params={"song": song["filepath"], "user": user})
                    await asyncio.sleep(0.3)
                    await tracker.poll_queue()

            else:
                log.warning(f"Comando desconhecido: {cmd}")
                await manager.send_to(ws, {
                    "type": "error",
                    "payload": {"msg": f"Comando desconhecido: {cmd}"},
                })

    except httpx.ConnectError:
        msg_text = f"PiKaraoke não está acessível em {pk_url}. Inicie o PiKaraoke primeiro."
        log.error(f"handle_command '{cmd}': {msg_text}")
        await manager.send_to(ws, {
            "type": "error",
            "payload": {"msg": msg_text},
        })
    except httpx.TimeoutException:
        msg_text = f"PiKaraoke demorou demais para responder ({pk_url})."
        log.error(f"handle_command '{cmd}': timeout")
        await manager.send_to(ws, {
            "type": "error",
            "payload": {"msg": msg_text},
        })
    except Exception as e:
        log.error(f"handle_command '{cmd}' error: {e}")
        await manager.send_to(ws, {
            "type": "error",
            "payload": {"msg": str(e)},
        })


# ---------------------------------------------------------------------------
# Detecção de tom musical (chama detect_keys.py como subprocess)
# ---------------------------------------------------------------------------

DETECT_KEYS_SCRIPT = "/home/shaddad/Documents/pikaraoke-songs/detect_keys.py"


async def _detect_and_update_key(filepath: str, song_id: int) -> None:
    """Detecta o tom musical de um arquivo e atualiza o banco. Roda em background."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, DETECT_KEYS_SCRIPT, "--filepath", filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        key = (stdout.decode().strip() if stdout else "")
        if key:
            log.info(f"Tom detectado pós-download: [{song_id}] {key} — {os.path.basename(filepath)}")
        else:
            log.debug(f"Sem tom detectado para [{song_id}] {os.path.basename(filepath)}: {stderr.decode()[:200]}")
    except asyncio.TimeoutError:
        log.warning(f"Timeout na detecção de tom: {filepath}")
    except Exception as e:
        log.warning(f"_detect_and_update_key error: {e}")


# ---------------------------------------------------------------------------
# Preview de música — servir áudio para escutar antes de enfileirar
# ---------------------------------------------------------------------------

def _db_get_song_for_preview(song_id: int):
    """Retorna {filepath, file_type} da música pelo ID, ou None."""
    conn = _db_connect()
    row = conn.execute(
        "SELECT filepath, file_type FROM songs WHERE id = ?", (song_id,)
    ).fetchone()
    conn.close()
    return row


def _extract_mp3_from_zip(filepath: str):
    """Extrai os bytes do MP3 de um ZIP CDG. Retorna bytes ou None."""
    try:
        with zipfile.ZipFile(filepath, "r") as z:
            mp3_names = [n for n in z.namelist() if n.lower().endswith(".mp3")]
            if not mp3_names:
                return None
            mp3_name = mp3_names[0]
            try:
                return z.read(mp3_name)
            except Exception:
                pass
        # Fallback: unzip externo (para deflate64 e outros algoritmos não suportados pelo módulo)
        import subprocess as _sp
        r = _sp.run(["unzip", "-p", filepath, mp3_name],
                    capture_output=True, timeout=30)
        if r.returncode == 0:
            return r.stdout
        return None
    except Exception as e:
        log.error(f"_extract_mp3_from_zip error: {e}")
        return None


def _range_file_response(filepath: str, request: Request) -> StreamingResponse:
    """Serve um arquivo de áudio com suporte a Range requests (seeking no browser)."""
    file_size = os.path.getsize(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    media_type = "audio/mp4" if ext in (".m4a", ".mp4") else "audio/mpeg"

    range_header = request.headers.get("range")
    if range_header:
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            def _iter_range(start: int, length: int):
                with open(filepath, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            return StreamingResponse(
                _iter_range(start, length),
                status_code=206,
                media_type=media_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                },
            )

    # Sem Range: serve o arquivo completo
    def _iter_full():
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter_full(),
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    asyncio.create_task(asyncio.to_thread(_db_populate_norms))
    tracker._system_volume = await asyncio.to_thread(set_system_volume, 70)
    log.info(f"Volume do sistema definido para: {tracker._system_volume}%")
    task = asyncio.create_task(polling_loop())
    log.info(f"PiKaraoke WS Bridge iniciado na porta {PORT}")
    log.info(f"Conectando ao PiKaraoke em {PIKARAOKE_URL}")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("PiKaraoke WS Bridge encerrado")


app = FastAPI(title="PiKaraoke WS Bridge", lifespan=lifespan)

# Arquivos estáticos
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/preview/{song_id}")
async def preview_song(song_id: int, request: Request):
    """Serve o áudio de uma música para preview no browser."""
    row = await asyncio.to_thread(_db_get_song_for_preview, song_id)
    if not row:
        raise HTTPException(status_code=404, detail="Música não encontrada")

    filepath = row["filepath"]
    file_type = row["file_type"]

    # Segurança: só serve arquivos dentro da pasta de músicas
    songs_base = os.path.realpath(DOWNLOAD_PATH)
    real_fp = os.path.realpath(filepath)
    if not real_fp.startswith(songs_base):
        raise HTTPException(status_code=403, detail="Acesso negado")

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no disco")

    if file_type == "zip":
        mp3_data = await asyncio.to_thread(_extract_mp3_from_zip, filepath)
        if not mp3_data:
            raise HTTPException(status_code=422, detail="MP3 não encontrado no ZIP")
        return Response(
            content=mp3_data,
            media_type="audio/mpeg",
            headers={"Accept-Ranges": "bytes", "Content-Disposition": "inline"},
        )
    else:
        # MP4/M4A: serve com suporte a Range para seeking
        return _range_file_response(filepath, request)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Deriva URL do PiKaraoke a partir do host usado pelo browser
    host_header = ws.headers.get("host", "")
    host_only = host_header.split(":")[0]  # remove a porta do WS (5001)
    pk_port = PIKARAOKE_URL.split(":")[-1]  # mantém a porta do PiKaraoke (5000)
    pk_url = f"http://{host_only}:{pk_port}" if host_only else PIKARAOKE_URL
    tracker.update_url(pk_url)

    await manager.connect(ws)
    # Envia estado completo imediatamente para o novo cliente
    await manager.send_to(ws, tracker.get_current_state())
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            await handle_command(ws, msg, pk_url)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WS error: {e}")
    finally:
        await manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

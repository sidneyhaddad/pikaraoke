"""
song_library.py — keeps karaoke.db in sync with the song folder.

On startup (and on every /refresh) the song folder is scanned and the
`songs` table is updated so it lists exactly the files that exist:

* rows whose file is gone are removed (unless a file with the same name
  exists somewhere else in the folder — then only the path is updated, so
  artist/title/key metadata is kept);
* files not yet in the table are added, with artist/title guessed from the
  file name ("Artist - Title---youtubeid.mp4");
* the FTS5 search index (songs_fts) is kept in step.

Play history, playlists and transposition cache are never touched.
The schema matches the one created by pikaraoke-ws (db_init), so either
side can create the database first.
"""
import logging
import os
import re
import sqlite3
import unicodedata

SONG_EXTENSIONS = {".mp4", ".mp3", ".zip", ".mkv", ".avi", ".webm", ".mov", ".m4a"}
DB_NAME = "karaoke.db"
# Folders never scanned (case-insensitive), besides hidden ones
SKIP_DIRS = {"excluido", "excluidos", "excluídos", "excluído", "deleted", "trash", "lixeira"}
# Safety: never let one sync delete more than this share of the songs table
# (protects the library when a drive is missing or a folder was moved).
MAX_REMOVE_RATIO = 0.5
# Database schema shipped in the repo (no songs: the database is built
# from scratch from this file and filled by scanning the song folder)
SCHEMA_SQL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "db", "schema.sql")

_YT_ID = re.compile(r"---[A-Za-z0-9_-]{6,20}(\.f\d+)?$")
_NOISE = re.compile(
    r"\s*[\(\[]\s*(karaok[eê][^\)\]]*|instrumental[^\)\]]*|lyrics?[^\)\]]*|"
    r"official[^\)\]]*|v[ií]deo[^\)\]]*|audio[^\)\]]*|hd|4k)\s*[\)\]]",
    re.IGNORECASE,
)


def normalize(text):
    """Lower-case and strip accents (same rule as pikaraoke-ws _normalize)."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def parse_filename(filename):
    """'Adele - Skyfall (Karaoke Version)---UadrtR56n6s.mp4' ->
    (clean_filename, artist, title, ext)."""
    stem, ext = os.path.splitext(filename)
    stem = _YT_ID.sub("", stem).strip()
    clean = _NOISE.sub("", stem).strip(" -_.") or stem
    artist, title = "", clean
    parts = clean.split(" - ", 1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip() and len(parts[0]) <= 60:
        artist, title = parts[0].strip(), parts[1].strip()
    return clean + ext.lower(), artist, title, ext.lstrip(".").lower()


def scan_folder(download_path):
    """All song files under download_path (recursive), as absolute posix paths."""
    found = []
    for root, dirs, files in os.walk(download_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        for name in files:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in SONG_EXTENSIONS:
                found.append(os.path.join(root, name).replace("\\", "/"))
    return found


def ensure_schema(conn):
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_filepath ON songs(filepath)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title_norm ON songs(title_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artist_norm ON songs(artist_norm)")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5(
            title_norm, artist_norm, tokenize='trigram'
        )
    """)


def sync_library(download_path, db_path=None):
    """Sync karaoke.db (default: <download_path>/karaoke.db) with the files in
    download_path. Returns the sorted list of song file paths."""
    files = scan_folder(download_path)
    on_disk = set(files)
    by_name = {}
    for f in files:
        by_name.setdefault(os.path.basename(f), f)

    db_path = db_path or os.path.join(download_path, DB_NAME)
    new_db = not os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    if new_db and os.path.exists(SCHEMA_SQL):
        with open(SCHEMA_SQL, encoding="utf-8") as fh:
            conn.executescript(fh.read())
        logging.info("Created %s from db/schema.sql" % db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, filepath, original_filename, clean_filename FROM songs"
        ).fetchall()

        known = set()
        moved, removed = [], []
        for r in rows:
            fp = r["filepath"]
            if fp in on_disk and fp not in known:
                known.add(fp)
                continue
            # file moved (e.g. songs copied from another machine): same file name
            new_fp = None
            for name in (os.path.basename(fp or ""), r["original_filename"], r["clean_filename"]):
                cand = by_name.get(name or "")
                if cand and cand not in known:
                    new_fp = cand
                    break
            if new_fp:
                known.add(new_fp)
                moved.append((new_fp, r["id"]))
            else:
                removed.append((r["id"],))

        # Safety net: a missing drive or emptied folder must not wipe the library
        if removed and (not files or len(removed) > MAX_REMOVE_RATIO * len(rows)):
            logging.warning(
                "Song library sync: %d of %d songs have no file on disk (%d files found) - "
                "NOT removing them (safety limit). Remove them manually if this is intended."
                % (len(removed), len(rows), len(files)))
            removed = []

        # original_filename records the file a song came from (e.g. the yt-dlp
        # download "Adele - Skyfall (Karaoke Version)---ID.mp4" of the entry
        # "Adele - Skyfall.mp4"). A new file with that name is the same song:
        # skip it instead of creating a duplicate (the column is also UNIQUE).
        removed_ids = {i for (i,) in removed}
        source_names = {r["original_filename"] for r in rows
                        if r["original_filename"] and r["id"] not in removed_ids}
        used_names = set(source_names)

        added, duplicates = [], 0
        for f in files:
            if f in known:
                continue
            name = os.path.basename(f)
            if name in source_names:
                duplicates += 1
                continue
            clean, artist, title, ext = parse_filename(name)
            if name in used_names:  # same file name in another folder
                base = os.path.relpath(f, download_path).replace("\\", "/")
                name, k = base, 2
                while name in used_names:
                    name, k = "%s#%d" % (base, k), k + 1
            used_names.add(name)
            try:
                size_kb = round(os.path.getsize(f) / 1024, 1)
            except OSError:
                size_kb = None
            added.append((name, clean, f, artist, title, ext, size_kb,
                          normalize(title), normalize(artist)))

        if moved:
            conn.executemany("UPDATE songs SET filepath=? WHERE id=?", moved)
        if removed:
            conn.executemany("DELETE FROM songs WHERE id=?", removed)
            conn.executemany("DELETE FROM songs_fts WHERE rowid=?", removed)
        for a in added:
            cur = conn.execute(
                "INSERT INTO songs (original_filename, clean_filename, filepath, artist, title,"
                " file_type, file_size_kb, has_vocals, title_norm, artist_norm)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)", a)
            conn.execute(
                "INSERT INTO songs_fts(rowid, title_norm, artist_norm) VALUES (?, ?, ?)",
                (cur.lastrowid, a[7], a[8]))
        conn.commit()
        if removed:
            conn.execute("VACUUM")

        logging.info(
            "Song library sync: %d files on disk, %d added, %d path updated, %d removed, "
            "%d skipped (source file of a song already in the library)"
            % (len(files), len(added), len(moved), len(removed), duplicates))
        songs = [r[0] for r in conn.execute(
            "SELECT filepath FROM songs ORDER BY lower(coalesce(clean_filename, filepath))")]
    finally:
        conn.close()
    return songs

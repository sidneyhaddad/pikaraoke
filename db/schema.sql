-- karaoke.db schema. The app creates <songs folder>/karaoke.db from this
-- file when it does not exist, then imports the songs found in the folder.
-- Keep in sync with pikaraoke-ws db_init().

CREATE TABLE IF NOT EXISTS songs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    original_filename  TEXT NOT NULL UNIQUE,
    clean_filename     TEXT NOT NULL,
    filepath           TEXT NOT NULL,
    artist             TEXT,
    title              TEXT,
    file_type          TEXT,
    youtube_id         TEXT,
    catalog_id         TEXT,
    duration_seconds   INTEGER,
    has_subtitles      BOOLEAN DEFAULT 0,
    verified           BOOLEAN DEFAULT 0,
    indexed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    musical_key        TEXT,
    key_analyzed_at    TIMESTAMP,

    first_phrase       TEXT,
    phrase_analyzed_at TIMESTAMP,
    phrase_source      TEXT
, has_vocals INTEGER NOT NULL DEFAULT 0, title_norm TEXT, artist_norm TEXT, file_size_kb REAL, publisher TEXT);

CREATE TABLE IF NOT EXISTS artist_map (
        norm_key   TEXT PRIMARY KEY,
        canonical  TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS play_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                filepath TEXT DEFAULT '',
                user TEXT DEFAULT '',
                played_at TEXT DEFAULT (datetime('now', 'localtime'))
            , semitones INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS transposed_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filepath TEXT NOT NULL,
                semitones INTEGER NOT NULL,
                transposed_filepath TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(original_filepath, semitones)
            );

CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'private',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

CREATE TABLE IF NOT EXISTS playlist_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                filepath TEXT NOT NULL,
                semitones INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                added_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(playlist_id, filepath),
                FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
            );

CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5(
                title_norm, artist_norm,
                tokenize='trigram'
            );

CREATE INDEX IF NOT EXISTS idx_artist          ON songs(artist);

CREATE INDEX IF NOT EXISTS idx_title           ON songs(title);

CREATE INDEX IF NOT EXISTS idx_key             ON songs(musical_key);

CREATE INDEX IF NOT EXISTS idx_key_pending     ON songs(key_analyzed_at)
    WHERE key_analyzed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_phrase_pending  ON songs(phrase_analyzed_at)
    WHERE phrase_analyzed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_ph_filepath ON play_history(filepath);

CREATE INDEX IF NOT EXISTS idx_ph_played_at ON play_history(played_at DESC);

CREATE INDEX IF NOT EXISTS idx_ph_user ON play_history(user);

CREATE INDEX IF NOT EXISTS idx_ts_lookup ON transposed_songs(original_filepath, semitones);

CREATE INDEX IF NOT EXISTS idx_title_norm ON songs(title_norm);

CREATE INDEX IF NOT EXISTS idx_artist_norm ON songs(artist_norm);

CREATE INDEX IF NOT EXISTS idx_ps_playlist ON playlist_songs(playlist_id, position);

CREATE INDEX IF NOT EXISTS idx_songs_filepath ON songs(filepath);

CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist);

CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title);

CREATE INDEX IF NOT EXISTS idx_songs_musical_key ON songs(musical_key);

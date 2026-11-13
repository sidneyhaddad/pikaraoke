"""Tests for lib/song_library.py — folder scan + karaoke.db sync."""
import os
import sqlite3

import pytest

from lib import song_library as sl

pytestmark = pytest.mark.unit


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x")


def songs(db):
    c = sqlite3.connect(db)
    try:
        return {r[0]: r[1:] for r in c.execute(
            "SELECT filepath, artist, title, original_filename FROM songs")}
    finally:
        c.close()


def fts_count(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT count(*) FROM songs_fts").fetchone()[0]
    finally:
        c.close()


def test_parse_filename_strips_youtube_id_and_karaoke_tag():
    clean, artist, title, ext = sl.parse_filename(
        "Adele - Skyfall (Karaoke Version)---UadrtR56n6s.mp4")
    assert (clean, artist, title, ext) == ("Adele - Skyfall.mp4", "Adele", "Skyfall", "mp4")


def test_new_database_is_created_from_schema_and_filled(tmp_path):
    touch(tmp_path / "Adele - Skyfall---UadrtR56n6s.mp4")
    touch(tmp_path / "sub" / "Queen - Bohemian Rhapsody.zip")
    touch(tmp_path / "notes.txt")

    result = sl.sync_library(str(tmp_path))

    db = tmp_path / "karaoke.db"
    assert db.exists()
    assert len(result) == 2
    rows = songs(db)
    assert ("Queen", "Bohemian Rhapsody") == rows[(tmp_path / "sub" / "Queen - Bohemian Rhapsody.zip").as_posix()][:2]
    assert fts_count(db) == 2
    # full schema came from db/schema.sql, not just the minimal fallback
    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert {"songs", "play_history", "playlists", "playlist_songs", "transposed_songs"} <= tables


def test_second_run_is_stable(tmp_path):
    touch(tmp_path / "A - One.mp4")
    sl.sync_library(str(tmp_path))
    sl.sync_library(str(tmp_path))
    assert len(songs(tmp_path / "karaoke.db")) == 1
    assert fts_count(tmp_path / "karaoke.db") == 1


def test_new_file_is_imported_and_deleted_file_removed(tmp_path):
    for n in ("A - One.mp4", "B - Two.mp4", "C - Three.mp4"):
        touch(tmp_path / n)
    sl.sync_library(str(tmp_path))
    os.remove(tmp_path / "C - Three.mp4")
    touch(tmp_path / "D - Four.mp4")

    result = sl.sync_library(str(tmp_path))

    names = sorted(os.path.basename(p) for p in result)
    assert names == ["A - One.mp4", "B - Two.mp4", "D - Four.mp4"]
    assert fts_count(tmp_path / "karaoke.db") == 3


def test_moved_file_keeps_metadata(tmp_path):
    touch(tmp_path / "A - One.mp4")
    sl.sync_library(str(tmp_path))
    db = tmp_path / "karaoke.db"
    c = sqlite3.connect(db)
    c.execute("UPDATE songs SET musical_key='Am'")
    c.commit()
    c.close()
    os.makedirs(tmp_path / "new")
    os.replace(tmp_path / "A - One.mp4", tmp_path / "new" / "A - One.mp4")

    sl.sync_library(str(tmp_path))

    c = sqlite3.connect(db)
    fp, key = c.execute("SELECT filepath, musical_key FROM songs").fetchone()
    c.close()
    assert fp.endswith("new/A - One.mp4") and key == "Am"


def test_source_file_of_existing_song_is_not_duplicated(tmp_path):
    touch(tmp_path / "Adele - Skyfall.mp4")
    sl.sync_library(str(tmp_path))
    db = tmp_path / "karaoke.db"
    c = sqlite3.connect(db)
    c.execute("UPDATE songs SET original_filename='Adele - Skyfall (Karaoke Version)---UadrtR56n6s.mp4'")
    c.commit()
    c.close()
    touch(tmp_path / "Adele - Skyfall (Karaoke Version)---UadrtR56n6s.mp4")

    result = sl.sync_library(str(tmp_path))

    assert len(result) == 1


def test_same_name_in_two_folders_is_imported_twice(tmp_path):
    touch(tmp_path / "x" / "A - One.mp4")
    touch(tmp_path / "y" / "A - One.mp4")
    assert len(sl.sync_library(str(tmp_path))) == 2


def test_safety_limit_keeps_library_when_folder_is_empty(tmp_path):
    for n in ("A - One.mp4", "B - Two.mp4"):
        touch(tmp_path / n)
    sl.sync_library(str(tmp_path))
    for n in ("A - One.mp4", "B - Two.mp4"):
        os.remove(tmp_path / n)          # e.g. songs drive not mounted

    sl.sync_library(str(tmp_path))

    assert len(songs(tmp_path / "karaoke.db")) == 2


def test_skipped_folders_are_not_imported(tmp_path):
    touch(tmp_path / "A - One.mp4")
    touch(tmp_path / "excluido" / "B - Two.mp4")
    touch(tmp_path / ".hidden" / "C - Three.mp4")
    assert [os.path.basename(p) for p in sl.sync_library(str(tmp_path))] == ["A - One.mp4"]


def test_transposed_copies_are_not_imported(tmp_path):
    touch(tmp_path / "A - One.zip")
    touch(tmp_path / "A - One_Tom-2.zip")
    touch(tmp_path / "A - One_Tom+3_Tom-1.zip")
    touch(tmp_path / "B - Two.mp4")
    sl.sync_library(str(tmp_path))
    c = sqlite3.connect(tmp_path / "karaoke.db")
    c.execute("INSERT INTO transposed_songs (original_filepath, semitones, transposed_filepath)"
              " VALUES (?, 4, ?)", ((tmp_path / "B - Two.mp4").as_posix(),
                                    (tmp_path / "B - Two up4.mp4").as_posix()))
    c.commit()
    c.close()
    touch(tmp_path / "B - Two up4.mp4")

    names = sorted(os.path.basename(p) for p in sl.sync_library(str(tmp_path)))

    assert names == ["A - One.zip", "B - Two.mp4"]


def test_history_and_playlists_are_never_touched(tmp_path):
    touch(tmp_path / "A - One.mp4")
    sl.sync_library(str(tmp_path))
    db = tmp_path / "karaoke.db"
    c = sqlite3.connect(db)
    c.execute("INSERT INTO play_history (title, filepath) VALUES ('One', 'gone.mp4')")
    c.execute("INSERT INTO playlists (name, owner) VALUES ('p', 'me')")
    c.commit()
    c.close()
    touch(tmp_path / "B - Two.mp4")

    sl.sync_library(str(tmp_path))

    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM play_history").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM playlists").fetchone()[0] == 1
    c.close()

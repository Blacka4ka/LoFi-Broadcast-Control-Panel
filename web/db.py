import os
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE = Path(os.getenv("LOFI_BASE", "/home/lofi/app"))
DB_PATH = BASE / "data" / "lofi.db"


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              email TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS password_resets (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              token_hash TEXT NOT NULL,
              expires_at INTEGER NOT NULL,
              used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracks (
              id INTEGER PRIMARY KEY,
              filename TEXT NOT NULL UNIQUE,
              enabled INTEGER NOT NULL DEFAULT 1,
              position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS videos (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              source_type TEXT NOT NULL CHECK(source_type IN ('file', 'remote')),
              source TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS schedules (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
              enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS text_overlays (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('text', 'datetime', 'location', 'weather')),
              content TEXT NOT NULL DEFAULT '',
              position TEXT NOT NULL DEFAULT 'bottom-left',
              offset_x INTEGER NOT NULL DEFAULT 30,
              offset_y INTEGER NOT NULL DEFAULT 30,
              font_size INTEGER NOT NULL DEFAULT 36,
              font_color TEXT NOT NULL DEFAULT 'white',
              font_family TEXT NOT NULL DEFAULT 'sans',
              timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
              enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS media_overlays (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              filename TEXT NOT NULL UNIQUE,
              position TEXT NOT NULL DEFAULT 'bottom-right',
              offset_x INTEGER NOT NULL DEFAULT 30,
              offset_y INTEGER NOT NULL DEFAULT 30,
              width INTEGER NOT NULL DEFAULT 420,
              interval_minutes INTEGER NOT NULL DEFAULT 0,
              duration_seconds INTEGER NOT NULL DEFAULT 10,
              enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(media_overlays)")
        }
        if "interval_minutes" not in columns:
            db.execute(
                "ALTER TABLE media_overlays "
                "ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 0"
            )
        if "duration_seconds" not in columns:
            db.execute(
                "ALTER TABLE media_overlays "
                "ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 10"
            )
        text_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(text_overlays)")
        }
        if "font_family" not in text_columns:
            db.execute(
                "ALTER TABLE text_overlays "
                "ADD COLUMN font_family TEXT NOT NULL DEFAULT 'sans'"
            )
        defaults = {
            "desired_state": "stopped",
            "rtmp_url": os.getenv("YT_URL", "") + (
                "/" + os.getenv("YT_KEY", "") if os.getenv("YT_KEY") else ""
            ),
            "video_bitrate": "3000k",
            "playlist_shuffle": "0",
            "config_nonce": "initial",
            "restart_nonce": "initial",
            "telegram_enabled": "0",
            "telegram_token": os.getenv("TG_TOKEN", ""),
            "telegram_user_id": os.getenv("TG_USER_ID", ""),
        }
        db.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", defaults.items()
        )
        # Migrate legacy YouTube RTMP URLs while preserving the stream key.
        db.execute(
            """UPDATE settings
               SET value = replace(
                 value,
                 'rtmp://a.rtmp.youtube.com/live2/',
                 'rtmps://a.rtmps.youtube.com/live2/'
               )
               WHERE key = 'rtmp_url'
                 AND value LIKE 'rtmp://a.rtmp.youtube.com/live2/%'"""
        )
        email = os.getenv("ADMIN_EMAIL")
        password = os.getenv("ADMIN_PASSWORD")
        if email and password:
            db.execute(
                "INSERT OR IGNORE INTO users(email, password_hash) VALUES(?, ?)",
                (email.lower(), generate_password_hash(password)),
            )
        db.commit()
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass


def setting(key, default=None):
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def update_settings(values):
    with connect() as db:
        db.executemany(
            """INSERT INTO settings(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            [(key, str(value)) for key, value in values.items()],
        )
        db.commit()

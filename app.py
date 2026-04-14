import io
import os
import re
import glob
import json
import uuid
import asyncio
import logging
import secrets
import sqlite3
import tempfile
import threading
import zipfile
import urllib.request
from datetime import datetime, timezone, timedelta, time as dt_time
from functools import wraps
from flask import Flask, request, redirect, render_template, jsonify, Response, session, url_for

import subprocess

import httpx

# Load .env file automatically.  Our own parser runs unconditionally so that
# empty-string env vars set by a misconfigured systemd unit or a stale shell
# session never silently swallow the real token from the .env file.
# python-dotenv (when installed) is also called as an enhancement; because
# our parser already filled in every non-empty value, the dotenv call is
# effectively a no-op for those keys.
#
# IMPORTANT: always resolve the .env path relative to this source file so the
# correct file is found regardless of the process working directory (systemd /
# Gunicorn may start with a different cwd).
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_DOTENV_PATH = os.path.join(_APP_DIR, ".env")


def _load_dotenv_fallback(dotenv_path: str) -> None:
    """Parse a .env file and inject variables into os.environ.

    A variable is written to os.environ when it is either absent from the
    environment OR currently set to an empty string.  This ensures that an
    empty-string placeholder injected by a misconfigured systemd
    ``Environment=`` directive (e.g. ``Environment="BOT_TOKEN="``) never
    hides the real token stored in the .env file.  A non-empty value that
    is already present in the environment is always preserved so that the
    runtime environment (systemd / Docker) can still override .env values.
    """
    if not os.path.isfile(dotenv_path):
        return
    try:
        with open(dotenv_path, encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip()
                # Strip a matched outer quote pair (e.g. "value" or 'value')
                if len(_val) >= 2 and _val[0] == _val[-1] and _val[0] in ('"', "'"):
                    _val = _val[1:-1]
                # Set if the key is absent OR the existing value is an empty string.
                # Using explicit `== ''` (not `not os.environ.get()`) so that
                # legitimate falsy-but-non-empty values such as '0' or 'false'
                # are preserved while empty-string placeholders injected by a
                # misconfigured systemd Environment= directive are overridden.
                if _key and (_key not in os.environ or os.environ[_key] == "") and _val:
                    os.environ[_key] = _val
    except Exception as _exc:
        # Log at debug level – never crash on .env parse failure
        logging.getLogger("app").debug("_load_dotenv_fallback: failed to parse %s: %s", dotenv_path, _exc)


# Always run our own reliable parser first so the empty-string fix is applied
# unconditionally regardless of whether python-dotenv is installed.
_load_dotenv_fallback(_DOTENV_PATH)
try:
    from dotenv import load_dotenv as _load_dotenv
    # override=False: python-dotenv will not touch vars already set by our
    # parser above (or by the real runtime environment).
    _load_dotenv(dotenv_path=_DOTENV_PATH, override=False)
except ImportError:
    pass  # python-dotenv not installed; our fallback above already handled it

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
logging.getLogger("httpx").setLevel(logging.WARNING)

log.info(".env path: %s (exists=%s)", _DOTENV_PATH, os.path.isfile(_DOTENV_PATH))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", os.environ.get("TOKEN", os.environ.get("BOT_TOKEN", "")))
log.info(
    "BOT_TOKEN detection: TELEGRAM_BOT_TOKEN=%s TOKEN=%s BOT_TOKEN=%s → resolved=%s",
    "set" if os.environ.get("TELEGRAM_BOT_TOKEN") else "unset",
    "set" if os.environ.get("TOKEN") else "unset",
    "set" if os.environ.get("BOT_TOKEN") else "unset",
    "set" if BOT_TOKEN else "EMPTY",
)
if not BOT_TOKEN and os.path.isfile(_DOTENV_PATH):
    log.warning(
        "Token is still empty even though .env exists at %s – check that the file contains "
        "TELEGRAM_BOT_TOKEN, TOKEN, or BOT_TOKEN and that it is readable by the service user.",
        _DOTENV_PATH,
    )
_admin_str = os.environ.get("ADMIN_CHAT_ID", os.environ.get("ADMIN_ID", ""))
ADMIN_ID = int(_admin_str) if _admin_str.isdigit() else None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
_secret_key = os.environ.get("SECRET_KEY", "")
if not _secret_key:
    _secret_key = "change-me-in-production-secret-key"
    log.warning("SECRET_KEY not set – using insecure default. Set SECRET_KEY env var in production!")
app.secret_key = _secret_key

# ===== AUTH CONFIG =====
OWNER_USER = os.environ.get("OWNER_USER", "admin")
OWNER_PASS = os.environ.get("OWNER_PASS", "admin123")
MANAGER_USER = os.environ.get("MANAGER_USER", "quanly")
MANAGER_PASS = os.environ.get("MANAGER_PASS", "quanly123")

# Absolute directory of this file – used to build robust file paths that do not
# depend on the process's current working directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))

ALBUMS_FILE = os.path.join(_APP_DIR, "albums.json")
SUBS_FILE = os.path.join(_APP_DIR, "subscribers.json")
CONFIG_FILE = os.path.join(_APP_DIR, "config.json")
BANNED_FILE = os.path.join(_APP_DIR, "banned.json")
BOTS_FILE = os.path.join(_APP_DIR, "bots.json")
MESSAGES_FILE = os.path.join(_APP_DIR, "messages.json")
STATS_FILE = os.path.join(_APP_DIR, "stats.json")
SLOGANS_FILE = os.path.join(_APP_DIR, "slogans.json")
DB_FILE = os.path.join(_APP_DIR, "data.db")

# ===== LOCAL STORAGE CONFIG =====
UPLOAD_BASE_DIR = os.path.join(_APP_DIR, "static", "uploads")
UPLOAD_IMAGES_DIR = os.path.join(UPLOAD_BASE_DIR, "images")
UPLOAD_VIDEOS_DIR = os.path.join(UPLOAD_BASE_DIR, "videos")
BACKUP_DIR = os.path.join(_APP_DIR, "static", "backups")

# Ensure upload directories exist at startup
for _d in (UPLOAD_IMAGES_DIR, UPLOAD_VIDEOS_DIR, BACKUP_DIR):
    os.makedirs(_d, exist_ok=True)


def load_json(file, default):
    try:
        with open(file, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("load_json(%s) failed – returning default. Error: %s", file, exc)
        return default


def save_json(file, data):
    tmp_path = None
    try:
        dir_name = os.path.dirname(os.path.abspath(file))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, file)
    except Exception as exc:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        log.error("save_json(%s) failed. Error: %s", file, exc)
        raise


# ===== SQLITE DATABASE (for ID management, future Turso migration) =====

def get_db() -> sqlite3.Connection:
    """Return a new SQLite connection with row_factory set.

    WAL mode allows concurrent readers and a single writer without exclusive
    file-level locks, preventing 'database is locked' errors when the bot
    process and the gunicorn broadcast threads both write at the same time.
    busy_timeout tells SQLite to retry for up to 5 s before raising an error.
    Each call creates a fresh connection bound to the calling thread, so no
    cross-thread sharing occurs.
    """
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS telegram_ids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_type TEXT NOT NULL DEFAULT 'ids',
                message TEXT,
                media_type TEXT,
                media_url TEXT,
                buttons_json TEXT,
                total_ids INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                bot_results_json TEXT,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_analytics (
                bot_id TEXT PRIMARY KEY,
                starts_count INTEGER DEFAULT 0,
                messages_sent INTEGER DEFAULT 0,
                interactions_count INTEGER DEFAULT 0,
                replies_count INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # --- New tables replacing JSON files ---
        conn.execute('''
            CREATE TABLE IF NOT EXISTS albums (
                id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL DEFAULT '{}'
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bots_config (
                id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL DEFAULT '{}',
                sort_order INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS subscribers (
                bot_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, user_id)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        conn.commit()
    _migrate_json_to_db()


def backup_db_to_bytes() -> bytes:
    """Safely copy the SQLite DB to bytes using SQLite backup API."""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(DB_FILE)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        src.close()
        dst.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _migrate_json_to_db():
    """One-time migration: import data from JSON files into SQLite tables.

    Each section only runs when the corresponding table is empty, so it is
    safe to call on every startup — subsequent calls are effectively no-ops.
    """
    try:
        with get_db() as conn:
            # ---- albums ----
            count = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
            if count == 0 and os.path.isfile(ALBUMS_FILE):
                try:
                    data = load_json(ALBUMS_FILE, {})
                    for album_id, album_data in data.items():
                        conn.execute(
                            "INSERT OR IGNORE INTO albums (id, data_json) VALUES (?, ?)",
                            (album_id, json.dumps(album_data, ensure_ascii=False)),
                        )
                    log.info("_migrate_json_to_db: migrated %d albums from %s", len(data), ALBUMS_FILE)
                except Exception as exc:
                    log.warning("_migrate_json_to_db: albums migration failed: %s", exc)

            # ---- bots_config ----
            count = conn.execute("SELECT COUNT(*) FROM bots_config").fetchone()[0]
            if count == 0 and os.path.isfile(BOTS_FILE):
                try:
                    data = load_json(BOTS_FILE, [])
                    for i, bot in enumerate(data):
                        conn.execute(
                            "INSERT OR IGNORE INTO bots_config (id, data_json, sort_order) VALUES (?, ?, ?)",
                            (bot.get("id", str(i)), json.dumps(bot, ensure_ascii=False), i),
                        )
                    log.info("_migrate_json_to_db: migrated %d bots from %s", len(data), BOTS_FILE)
                except Exception as exc:
                    log.warning("_migrate_json_to_db: bots migration failed: %s", exc)

            # ---- app_config (schedule config) ----
            row = conn.execute("SELECT 1 FROM app_config WHERE key='config'").fetchone()
            if row is None and os.path.isfile(CONFIG_FILE):
                try:
                    data = load_json(CONFIG_FILE, {"hour": 0, "minute": 0})
                    conn.execute(
                        "INSERT OR IGNORE INTO app_config (key, value) VALUES ('config', ?)",
                        (json.dumps(data, ensure_ascii=False),),
                    )
                    log.info("_migrate_json_to_db: migrated config from %s", CONFIG_FILE)
                except Exception as exc:
                    log.warning("_migrate_json_to_db: config migration failed: %s", exc)

            # ---- messages flow ----
            row = conn.execute("SELECT 1 FROM app_config WHERE key='messages_flow'").fetchone()
            if row is None and os.path.isfile(MESSAGES_FILE):
                try:
                    data = load_json(MESSAGES_FILE, {})
                    if data:
                        conn.execute(
                            "INSERT OR IGNORE INTO app_config (key, value) VALUES ('messages_flow', ?)",
                            (json.dumps(data, ensure_ascii=False),),
                        )
                        log.info("_migrate_json_to_db: migrated messages flow from %s", MESSAGES_FILE)
                except Exception as exc:
                    log.warning("_migrate_json_to_db: messages migration failed: %s", exc)

            # ---- slogans ----
            row = conn.execute("SELECT 1 FROM app_config WHERE key='slogans'").fetchone()
            if row is None and os.path.isfile(SLOGANS_FILE):
                try:
                    data = load_json(SLOGANS_FILE, {"enabled": True, "items": []})
                    conn.execute(
                        "INSERT OR IGNORE INTO app_config (key, value) VALUES ('slogans', ?)",
                        (json.dumps(data, ensure_ascii=False),),
                    )
                    log.info("_migrate_json_to_db: migrated slogans from %s", SLOGANS_FILE)
                except Exception as exc:
                    log.warning("_migrate_json_to_db: slogans migration failed: %s", exc)

            # ---- subscribers (global + per-bot) ----
            count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
            if count == 0:
                # Global subscribers.json → bot_id='global'
                if os.path.isfile(SUBS_FILE):
                    try:
                        subs = load_json(SUBS_FILE, [])
                        for uid in subs:
                            conn.execute(
                                "INSERT OR IGNORE INTO subscribers (bot_id, user_id) VALUES ('global', ?)",
                                (int(uid),),
                            )
                        log.info("_migrate_json_to_db: migrated %d global subscribers", len(subs))
                    except Exception as exc:
                        log.warning("_migrate_json_to_db: global subscribers migration failed: %s", exc)
                # Per-bot subs_*.json
                for path in glob.glob(os.path.join(_APP_DIR, "subs_*.json")):
                    bot_id = os.path.basename(path).removeprefix("subs_").removesuffix(".json")
                    try:
                        subs = load_json(path, [])
                        for uid in subs:
                            conn.execute(
                                "INSERT OR IGNORE INTO subscribers (bot_id, user_id) VALUES (?, ?)",
                                (bot_id, int(uid)),
                            )
                        log.info("_migrate_json_to_db: migrated %d subscribers for bot %s", len(subs), bot_id)
                    except Exception as exc:
                        log.warning("_migrate_json_to_db: subs migration for %s failed: %s", bot_id, exc)

            # ---- banned users ----
            count = conn.execute("SELECT COUNT(*) FROM banned_users").fetchone()[0]
            if count == 0 and os.path.isfile(BANNED_FILE):
                try:
                    banned = load_json(BANNED_FILE, [])
                    for uid in banned:
                        conn.execute(
                            "INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)",
                            (int(uid),),
                        )
                    log.info("_migrate_json_to_db: migrated %d banned users from %s", len(banned), BANNED_FILE)
                except Exception as exc:
                    log.warning("_migrate_json_to_db: banned migration failed: %s", exc)

            conn.commit()
    except Exception as exc:
        log.error("_migrate_json_to_db: unexpected error: %s", exc, exc_info=True)


# ===== DB HELPER FUNCTIONS (replacing JSON file read/write) =====

def db_get_albums() -> dict:
    """Load all albums from SQLite. Returns dict keyed by album_id."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT id, data_json FROM albums").fetchall()
        return {row["id"]: json.loads(row["data_json"]) for row in rows}
    except Exception as exc:
        log.error("db_get_albums failed: %s", exc)
        return {}


def db_save_album(album_id: str, album_data: dict):
    """Insert or replace a single album in SQLite."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO albums (id, data_json) VALUES (?, ?)",
            (album_id, json.dumps(album_data, ensure_ascii=False)),
        )
        conn.commit()


def db_delete_album(album_id: str):
    """Delete an album from SQLite."""
    with get_db() as conn:
        conn.execute("DELETE FROM albums WHERE id=?", (album_id,))
        conn.commit()


def db_get_bots() -> list:
    """Load bots config list from SQLite."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT data_json FROM bots_config ORDER BY sort_order"
            ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]
    except Exception as exc:
        log.error("db_get_bots failed: %s", exc)
        return []


def db_save_bots(bots: list):
    """Replace the entire bots config list atomically."""
    with get_db() as conn:
        conn.execute("DELETE FROM bots_config")
        for i, bot in enumerate(bots):
            conn.execute(
                "INSERT INTO bots_config (id, data_json, sort_order) VALUES (?, ?, ?)",
                (bot.get("id", str(i)), json.dumps(bot, ensure_ascii=False), i),
            )
        conn.commit()


def db_get_config() -> dict:
    """Load schedule config {hour, minute} from SQLite."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key='config'"
            ).fetchone()
        if row:
            return json.loads(row["value"])
        return {"hour": 0, "minute": 0}
    except Exception as exc:
        log.error("db_get_config failed: %s", exc)
        return {"hour": 0, "minute": 0}


def db_save_config(cfg: dict):
    """Save schedule config to SQLite."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES ('config', ?)",
            (json.dumps(cfg, ensure_ascii=False),),
        )
        conn.commit()


def db_get_messages_flow_raw() -> dict:
    """Load raw messages flow dict from SQLite (no migration/defaults applied)."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key='messages_flow'"
            ).fetchone()
        if row:
            return json.loads(row["value"])
        return {}
    except Exception as exc:
        log.error("db_get_messages_flow_raw failed: %s", exc)
        return {}


def db_save_messages_flow(flow: dict):
    """Save messages flow to SQLite."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES ('messages_flow', ?)",
            (json.dumps(flow, ensure_ascii=False),),
        )
        conn.commit()


def db_get_subscribers(bot_id: str) -> list:
    """Load subscriber user_id list for a given bot_id from SQLite."""
    db_key = "global" if not bot_id or bot_id == "env_default" else bot_id
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM subscribers WHERE bot_id=?", (db_key,)
            ).fetchall()
        return [row["user_id"] for row in rows]
    except Exception as exc:
        log.error("db_get_subscribers(%s) failed: %s", bot_id, exc)
        return []


def db_add_subscriber(bot_id: str, user_id: int):
    """Add a subscriber to SQLite (idempotent)."""
    db_key = "global" if not bot_id or bot_id == "env_default" else bot_id
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (bot_id, user_id) VALUES (?, ?)",
            (db_key, user_id),
        )
        conn.commit()


def db_remove_subscriber(bot_id: str, user_id: int):
    """Remove a subscriber from SQLite."""
    db_key = "global" if not bot_id or bot_id == "env_default" else bot_id
    with get_db() as conn:
        conn.execute(
            "DELETE FROM subscribers WHERE bot_id=? AND user_id=?",
            (db_key, user_id),
        )
        conn.commit()


def db_save_subscribers(bot_id: str, subs: list):
    """Atomically replace the subscriber list for a bot."""
    db_key = "global" if not bot_id or bot_id == "env_default" else bot_id
    with get_db() as conn:
        conn.execute("DELETE FROM subscribers WHERE bot_id=?", (db_key,))
        for uid in subs:
            conn.execute(
                "INSERT INTO subscribers (bot_id, user_id) VALUES (?, ?)",
                (db_key, int(uid)),
            )
        conn.commit()


def db_get_all_subscriber_ids() -> set:
    """Return the set of all subscriber user_ids (as strings) across all bots."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT DISTINCT user_id FROM subscribers").fetchall()
        return {str(row["user_id"]) for row in rows}
    except Exception as exc:
        log.error("db_get_all_subscriber_ids failed: %s", exc)
        return set()


def db_get_slogans() -> dict:
    """Load slogans config from SQLite."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key='slogans'"
            ).fetchone()
        if row:
            return json.loads(row["value"])
        return {"enabled": True, "items": []}
    except Exception as exc:
        log.error("db_get_slogans failed: %s", exc)
        return {"enabled": True, "items": []}


def db_save_slogans(data: dict):
    """Save slogans config to SQLite."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES ('slogans', ?)",
            (json.dumps(data, ensure_ascii=False),),
        )
        conn.commit()


def db_get_banned() -> list:
    """Load banned user_id list from SQLite."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT user_id FROM banned_users").fetchall()
        return [row["user_id"] for row in rows]
    except Exception as exc:
        log.error("db_get_banned failed: %s", exc)
        return []


def db_add_ban(user_id: int):
    """Add a user to the ban list (idempotent)."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,)
        )
        conn.commit()


def db_remove_ban(user_id: int):
    """Remove a user from the ban list."""
    with get_db() as conn:
        conn.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        conn.commit()


def db_export_as_json() -> dict:
    """Export all DB-stored data as a dict mapping filename → JSON bytes.
    Used by the backup function to produce human-readable JSON exports."""
    result = {}
    try:
        albums = db_get_albums()
        result["albums.json"] = json.dumps(albums, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    try:
        bots = db_get_bots()
        result["bots.json"] = json.dumps(bots, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    try:
        cfg = db_get_config()
        result["config.json"] = json.dumps(cfg, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    try:
        flow = db_get_messages_flow_raw()
        result["messages.json"] = json.dumps(flow, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    try:
        slogans = db_get_slogans()
        result["slogans.json"] = json.dumps(slogans, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    try:
        # Group subscribers by bot_id
        with get_db() as conn:
            rows = conn.execute("SELECT bot_id, user_id FROM subscribers ORDER BY bot_id").fetchall()
        subs_by_bot: dict = {}
        for row in rows:
            subs_by_bot.setdefault(row["bot_id"], []).append(row["user_id"])
        global_subs = subs_by_bot.pop("global", [])
        result["subscribers.json"] = json.dumps(global_subs, indent=2, ensure_ascii=False).encode("utf-8")
        for bid, subs in subs_by_bot.items():
            result[f"subs_{bid}.json"] = json.dumps(subs, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    try:
        banned = db_get_banned()
        result["banned.json"] = json.dumps(banned, indent=2, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    return result


# Broadcast task state (module-level so it persists across requests)
_broadcast_lock = threading.Lock()
_broadcast_status: dict = {
    "running": False, "total": 0, "done": 0,
    "active": 0, "blocked": 0, "invalid": 0, "error": 0,
    "bot_results": {},  # {bot_id: {"name": str, "success": int, "fail": int}}
}

# Broadcast All task state (global broadcast to active IDs with rich media)
_broadcast_all_lock = threading.Lock()
_broadcast_all_status: dict = {
    "running": False, "total": 0, "done": 0,
    "success": 0, "fail": 0,
    "bot_results": {},  # {bot_id: {"name": str, "success": int, "fail": int}}
}


# Initialize DB on startup
init_db()


class _BotManagerStub:
    """Minimal placeholder before the real BotManager is instantiated."""
    def notify_change(self):
        pass
    def start_in_thread(self):
        pass


# Will be replaced with a real _BotManager instance once the bot block runs
_bot_manager: _BotManagerStub = _BotManagerStub()


def total_posts(albums):
    return sum(len(a.get("posts", [])) for a in albums.values() if isinstance(a, dict))


def subs_file(bot_id: str) -> str:
    """Return per-bot subscriber file path, falling back to global file."""
    if not bot_id or bot_id == "env_default":
        return SUBS_FILE
    return os.path.join(_APP_DIR, f"subs_{bot_id}.json")


def increment_bot_stat(bot_id: str, field: str, count: int = 1):
    """Atomically increment a stat field in bot_analytics table."""
    valid_fields = ("starts_count", "messages_sent", "interactions_count", "replies_count")
    if field not in valid_fields or count <= 0:
        return
    try:
        with get_db() as conn:
            conn.execute(
                f"""INSERT INTO bot_analytics (bot_id, {field}, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(bot_id) DO UPDATE SET
                        {field} = {field} + excluded.{field},
                        updated_at = CURRENT_TIMESTAMP""",
                (bot_id, count),
            )
            conn.commit()
    except Exception as e:
        log.warning("increment_bot_stat failed for %s.%s: %s", bot_id, field, e)


def get_all_analytics() -> list:
    """Return analytics rows for all bots, merged with bot names from DB."""
    bots_cfg = db_get_bots()
    name_map = {b.get("id", ""): b.get("name", "?") for b in bots_cfg}
    if BOT_TOKEN and "env_default" not in name_map:
        name_map["env_default"] = "Default Bot"
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT bot_id, starts_count, messages_sent, interactions_count, replies_count FROM bot_analytics"
            ).fetchall()
        analytics = []
        seen = set()
        for row in rows:
            bid = row["bot_id"]
            seen.add(bid)
            analytics.append({
                "bot_id": bid,
                "name": name_map.get(bid, bid),
                "starts_count": row["starts_count"],
                "messages_sent": row["messages_sent"],
                "interactions_count": row["interactions_count"],
                "replies_count": row["replies_count"],
            })
        # Add bots with no analytics yet
        for bid, bname in name_map.items():
            if bid not in seen:
                analytics.append({
                    "bot_id": bid,
                    "name": bname,
                    "starts_count": 0,
                    "messages_sent": 0,
                    "interactions_count": 0,
                    "replies_count": 0,
                })
        return analytics
    except Exception as e:
        log.warning("get_all_analytics failed: %s", e)
        return []


def increment_messages_sent(bot_id: str, count: int):
    """Increment the messages-sent counter for a bot in the analytics DB."""
    if count <= 0:
        return
    increment_bot_stat(bot_id, "messages_sent", count)


_DEFAULT_ALBUM_END_NODE = {
    "text": "Bạn đã xem xong album! 😊\nNhấn nút bên dưới để quay lại danh sách.",
    "buttons": [{"label": "🔙 Quay lại danh sách Album", "type": "open_album_list", "value": "menu"}],
}


def get_messages_flow() -> dict:
    """Load message flow from SQLite in node-based format.
    Automatically migrates from the old flat {start_text, buttons[]} format."""
    data = db_get_messages_flow_raw()
    # Detect old flat format (has "start_text" key) – may be encountered when
    # migrating from a JSON file that stored the old format.
    if isinstance(data, dict) and "start_text" in data:
        old_buttons = data.get("buttons", [])
        new_buttons = []
        for btn in old_buttons:
            label = str(btn.get("text", "")).strip()
            url = str(btn.get("url") or "").strip()
            cb = str(btn.get("callback_data") or "").strip()
            if label and url:
                new_buttons.append({"label": label, "type": "url", "value": url})
            elif label and cb:
                new_buttons.append({"label": label, "type": "node", "value": cb})
        flow = {"start": {"text": data.get("start_text", "Chào mừng bạn! 👋"), "buttons": new_buttons}}
        db_save_messages_flow(flow)
        return flow
    if not isinstance(data, dict) or not data:
        return {"start": {"text": "Chào mừng bạn! 👋", "buttons": []},
                "album_end": dict(_DEFAULT_ALBUM_END_NODE)}
    if "start" not in data:
        data["start"] = {"text": "Chào mừng bạn! 👋", "buttons": []}
    if "album_end" not in data:
        data["album_end"] = dict(_DEFAULT_ALBUM_END_NODE)
    return data


def _backup_files_to_zip(zf: zipfile.ZipFile):
    """Write all data into a ZipFile: SQLite DB + JSON exports of every table."""
    # Export DB tables as human-readable JSON files
    for fname, data_bytes in db_export_as_json().items():
        zf.writestr(fname, data_bytes)
    # Include the raw SQLite database for a full binary restore
    if os.path.exists(DB_FILE):
        try:
            zf.writestr(os.path.basename(DB_FILE), backup_db_to_bytes())
        except Exception as e:
            log.warning(f"Could not backup SQLite DB: {e}")


# ===== AUTH HELPERS =====

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "owner":
            return jsonify({"ok": False, "error": "Không có quyền thực hiện thao tác này"}), 403
        return f(*args, **kwargs)
    return decorated


# ===== LOCAL STORAGE HELPERS =====

def save_file_locally(file_stream, filename: str, file_type: str) -> str:
    """Save an uploaded file to the local static/uploads directory.

    Returns the URL path (e.g. /static/uploads/images/abc123.jpg).
    """
    ext = os.path.splitext(filename)[1].lower() or (".mp4" if file_type == "video" else ".jpg")
    unique_name = f"{uuid.uuid4().hex}{ext}"
    if file_type == "video":
        save_dir = UPLOAD_VIDEOS_DIR
    else:
        save_dir = UPLOAD_IMAGES_DIR
    save_path = os.path.join(save_dir, unique_name)
    with open(save_path, "wb") as f:
        f.write(file_stream.read())
    return f"/static/uploads/{'videos' if file_type == 'video' else 'images'}/{unique_name}"


def process_video_to_hls(input_video_path: str, output_dir: str) -> str:
    """Convert a video file to HLS format using FFmpeg.

    Creates index.m3u8 and .ts segment files in output_dir.
    Returns the URL path to the .m3u8 playlist file, or raises RuntimeError on failure.
    """
    os.makedirs(output_dir, exist_ok=True)
    m3u8_path = os.path.join(output_dir, "index.m3u8")
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video_path,
        "-profile:v", "baseline",
        "-level", "3.0",
        "-s", "640x360",
        "-start_number", "0",
        "-hls_time", "10",
        "-hls_list_size", "0",
        "-f", "hls",
        m3u8_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        log.error(f"FFmpeg HLS conversion failed: {exc.stderr.decode(errors='replace')}")
        raise RuntimeError("FFmpeg conversion failed") from exc
    except FileNotFoundError:
        log.warning("ffmpeg not found – serving original video without HLS conversion")
        raise RuntimeError("ffmpeg not installed")
    # Construct URL from known components to avoid platform-specific path issues
    file_id = os.path.basename(output_dir)
    return f"/static/uploads/videos/{file_id}/index.m3u8"


def run_daily_backup():
    """Package JSON data files into a zip and save to local backups/ folder.
    Keeps only the latest backup by removing the previous day's file."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    backup_filename = f"backup_{today_str}.zip"
    old_backup_filename = f"backup_{yesterday_str}.zip"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    old_backup_path = os.path.join(BACKUP_DIR, old_backup_filename)

    # Delete yesterday's backup
    try:
        if os.path.exists(old_backup_path):
            os.remove(old_backup_path)
            log.info(f"Deleted old backup: {old_backup_path}")
    except Exception as e:
        log.warning(f"Could not delete old backup {old_backup_path}: {e}")

    # Build zip and save locally
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        _backup_files_to_zip(zf)

    log.info(f"Backup saved locally: {backup_path}")
    return f"/static/backups/{backup_filename}"


# ===== FLASK ROUTES =====

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        # Use constant-time comparison to prevent timing attacks
        is_owner = (secrets.compare_digest(username, OWNER_USER) and
                    secrets.compare_digest(password, OWNER_PASS))
        is_manager = (secrets.compare_digest(username, MANAGER_USER) and
                      secrets.compare_digest(password, MANAGER_PASS))
        if is_owner or is_manager:
            session["logged_in"] = True
            session["role"] = "owner" if is_owner else "manager"
            session["username"] = username
            # Validate next_url to prevent open redirect: only allow relative paths
            next_url = request.args.get("next", "")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect("/")
        else:
            error = "Sai tên đăng nhập hoặc mật khẩu!"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    all_albums = db_get_albums()
    subs = db_get_subscribers("global")
    bots = db_get_bots()
    messages_cfg = get_messages_flow()
    cfg = db_get_config()
    slogans = db_get_slogans()
    active_album = request.args.get("album", "")
    # ID stats from SQLite
    id_stats = {"total": 0, "active": 0, "blocked": 0, "invalid": 0, "unknown": 0}
    try:
        with get_db() as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM telegram_ids GROUP BY status"
            ):
                id_stats[row["status"]] = row["cnt"]
                id_stats["total"] += row["cnt"]
    except Exception:
        pass
    # Paginate albums: 20 per page
    ALBUMS_PER_PAGE = 20
    sorted_keys = sorted(all_albums.keys())
    total_albums = len(sorted_keys)
    total_pages = max(1, (total_albums + ALBUMS_PER_PAGE - 1) // ALBUMS_PER_PAGE)
    try:
        page = max(1, min(int(request.args.get("page", 1)), total_pages))
    except (ValueError, TypeError):
        page = 1
    start = (page - 1) * ALBUMS_PER_PAGE
    end = start + ALBUMS_PER_PAGE
    paginated_keys = sorted_keys[start:end]
    albums = {k: all_albums[k] for k in paginated_keys}
    return render_template("index.html", albums=albums, subs=subs,
                           active_album=active_album, total_posts=total_posts(all_albums),
                           role=session.get("role", ""), username=session.get("username", ""),
                           bots=bots, messages_cfg=messages_cfg, cfg=cfg, slogans=slogans,
                           id_stats=id_stats, page=page, total_pages=total_pages,
                           total_albums=total_albums)


# --- Album management ---

@app.route("/albums/create", methods=["POST"])
@login_required
def create_album():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title:
        return "Missing title", 400
    album_id = f"album_{uuid.uuid4().hex[:8]}"
    db_save_album(album_id, {
        "title": title,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "posts": [],
    })
    return redirect(f"/?album={album_id}")


@app.route("/albums/<album_id>/delete", methods=["POST"])
@owner_required
def delete_album(album_id):
    db_delete_album(album_id)
    return redirect("/")


# --- Post management ---

@app.route("/albums/<album_id>/posts", methods=["POST"])
@login_required
def add_post(album_id):
    caption = request.form.get("caption", "").strip()
    albums = db_get_albums()
    if album_id not in albums:
        return "Album not found", 404
    media_items = []
    # Accept both 'media' (new) and 'images' (legacy) field names
    if "media" in request.files:
        files = request.files.getlist("media")
    else:
        files = request.files.getlist("images")
    for f in files:
        if f and f.filename:
            try:
                mime = (f.content_type or "").lower()
                item_type = "video" if mime.startswith("video/") else "image"
                url = save_file_locally(f.stream, f.filename, item_type)
                if item_type == "video":
                    file_id = os.path.splitext(os.path.basename(url))[0]
                    hls_dir = os.path.join(UPLOAD_VIDEOS_DIR, file_id)
                    src_path = url.lstrip("/")
                    try:
                        url = process_video_to_hls(src_path, hls_dir)
                    except RuntimeError:
                        pass  # fall back to serving original video
                media_items.append({"url": url, "type": item_type})
            except Exception as e:
                log.error(f"Local upload failed: {e}")
    post = {
        "id": uuid.uuid4().hex,
        "caption": caption,
        "photos": media_items,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    album = albums[album_id]
    if "posts" not in album:
        album["posts"] = []
    album["posts"].append(post)
    db_save_album(album_id, album)
    return redirect(f"/?album={album_id}")


@app.route("/albums/<album_id>/posts/<post_id>/delete", methods=["POST"])
@owner_required
def delete_post(album_id, post_id):
    albums = db_get_albums()
    if album_id in albums:
        albums[album_id]["posts"] = [
            p for p in albums[album_id].get("posts", []) if p["id"] != post_id
        ]
        db_save_album(album_id, albums[album_id])
    return redirect(f"/?album={album_id}")


# --- Legacy routes (kept for backward compatibility) ---

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    album_id = request.form.get("album_id", "").strip()
    caption_text = request.form.get("caption", "").strip()
    if not album_id:
        return "Missing album_id", 400
    albums = db_get_albums()
    if album_id not in albums:
        albums[album_id] = {
            "title": album_id,
            "description": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "posts": [],
        }
    photos = []
    for img in request.files.getlist("images"):
        if img and img.filename:
            try:
                mime = (img.content_type or "").lower()
                item_type = "video" if mime.startswith("video/") else "image"
                url = save_file_locally(img.stream, img.filename, item_type)
                if item_type == "video":
                    # Build HLS output dir from the saved file path
                    file_id = os.path.splitext(os.path.basename(url))[0]
                    hls_dir = os.path.join(UPLOAD_VIDEOS_DIR, file_id)
                    src_path = url.lstrip("/")
                    try:
                        url = process_video_to_hls(src_path, hls_dir)
                    except RuntimeError:
                        pass  # fall back to serving original video
                photos.append({"url": url, "type": item_type})
            except Exception as e:
                log.error(f"Local upload failed: {e}")
    if photos:
        post = {
            "id": uuid.uuid4().hex,
            "caption": caption_text,
            "photos": photos,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        albums[album_id].setdefault("posts", []).append(post)
    db_save_album(album_id, albums[album_id])
    return redirect("/")


@app.route("/delete/<album_id>")
@owner_required
def delete(album_id):
    db_delete_album(album_id)
    return redirect("/")


@app.route("/healthz")
def healthz():
    return "OK", 200


@app.route("/stream/<file_id>")
def stream_video(file_id):
    """Serve the HLS player page for a given video file_id."""
    # Validate file_id: only allow hex UUIDs (32 hex chars) to prevent path traversal
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{32}", file_id):
        return "Invalid file ID", 400
    hls_dir = os.path.join(UPLOAD_VIDEOS_DIR, file_id)
    if not os.path.isdir(hls_dir):
        return "Stream not found", 404
    m3u8_url = f"/static/uploads/videos/{file_id}/index.m3u8"
    return render_template("player.html", m3u8_url=m3u8_url)


@app.route("/backup/download")
@owner_required
def backup_download():
    """Owner-only endpoint to download a manual backup of all JSON data files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _backup_files_to_zip(zf)
    buf.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Response(
        buf.read(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=backup_{timestamp}.zip"},
    )


@app.route("/backup/manual", methods=["POST"])
@owner_required
def backup_manual():
    """Trigger a manual backup and save locally, returning the download URL."""
    try:
        url = run_daily_backup()
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        log.error(f"Manual backup failed: {e}")
        return jsonify({"ok": False, "error": "Sao lưu thất bại. Vui lòng thử lại."}), 500


@app.route("/backup/restore", methods=["POST"])
@owner_required
def backup_restore():
    """Restore JSON data files from the most recent local backup zip."""
    backup_zip_path = None
    for days_ago in range(0, 7):
        date_str = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        candidate = os.path.join(BACKUP_DIR, f"backup_{date_str}.zip")
        if os.path.exists(candidate):
            backup_zip_path = candidate
            break

    if not backup_zip_path:
        return jsonify({"ok": False, "error": "Không tìm thấy bản sao lưu cục bộ"}), 404

    try:
        db_basename = os.path.basename(DB_FILE)

        with zipfile.ZipFile(backup_zip_path, "r") as zf:
            for entry in zf.namelist():
                fname = os.path.basename(entry)
                if fname == db_basename:
                    # Restore the raw SQLite DB file and reinitialise tables
                    db_data = zf.read(entry)
                    with open(DB_FILE, "wb") as f:
                        f.write(db_data)
                    init_db()
                    continue
                # Restore JSON exports back into the DB (for backups that include
                # human-readable JSON exports alongside the binary DB file).
                try:
                    content = json.loads(zf.read(entry).decode("utf-8"))
                except Exception:
                    continue
                if fname == "albums.json" and isinstance(content, dict):
                    for aid, adata in content.items():
                        db_save_album(aid, adata)
                elif fname == "bots.json" and isinstance(content, list):
                    db_save_bots(content)
                elif fname == "config.json" and isinstance(content, dict):
                    db_save_config(content)
                elif fname == "messages.json" and isinstance(content, dict):
                    db_save_messages_flow(content)
                elif fname == "slogans.json" and isinstance(content, dict):
                    db_save_slogans(content)
                elif fname == "subscribers.json" and isinstance(content, list):
                    db_save_subscribers("global", content)
                elif fname.startswith("subs_") and fname.endswith(".json") and isinstance(content, list):
                    bot_id_key = fname.removeprefix("subs_").removesuffix(".json")
                    db_save_subscribers(bot_id_key, content)
                elif fname in ("banned.json", "banned_users.json") and isinstance(content, list):
                    with get_db() as conn:
                        for uid in content:
                            conn.execute(
                                "INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (int(uid),)
                            )
                        conn.commit()

        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Restore failed: {e}")
        return jsonify({"ok": False, "error": "Khôi phục thất bại. Vui lòng thử lại."}), 500


async def _async_run_ids_broadcast(
    bots: list, message: str, user_ids: list,
    media_type: str = "none", media_url: str = "", buttons: list | None = None,
    media_bytes: bytes = None, media_filename: str = None,
) -> None:
    """Async broadcast to IDs with all bots running concurrently.

    Every active bot processes the full list of user_ids in parallel.  Per-bot
    proactive rate limit: ~25 msg/sec (asyncio.sleep(0.04)).  When a 429 is
    received, only the affected bot sleeps for the Telegram-supplied
    ``retry_after`` duration; all other bots continue uninterrupted.

    Supports rich media (image / video via URL or file upload) and inline
    keyboard buttons.

    After *all* bots have attempted a given uid, the best reachability status
    (active > blocked > invalid) is written to the DB and the global progress
    counter is incremented.
    """
    if buttons is None:
        buttons = []
    active_bots = [b for b in bots if (b.get("token") or "").strip()]
    num_active = len(active_bots)
    if num_active == 0:
        with _broadcast_lock:
            _broadcast_status["running"] = False
        return

    bot_res: dict = {
        b.get("id", ""): {"name": b.get("name", "Bot"), "success": 0, "fail": 0}
        for b in bots
    }
    uid_status_map: dict = {uid: None for uid in user_ids}  # None = undetermined
    uid_done_count: dict = {uid: 0 for uid in user_ids}
    stats_lock = asyncio.Lock()
    file_id_cache: dict = {"id": None}

    async def bot_worker(bot: dict) -> None:
        token = (bot.get("token") or "").strip()
        bot_id = bot.get("id", "")
        if not token:
            return
        retry_until = 0.0  # Event-loop clock time after which this bot's 429 cooldown ends

        # 30s timeout accommodates multipart file uploads which are larger than plain JSON sends
        async with httpx.AsyncClient(timeout=30) as client:
            for uid in user_ids:
                # Wait out any active 429 cooldown for this bot
                now = asyncio.get_running_loop().time()
                if retry_until > now:
                    await asyncio.sleep(retry_until - now)

                # Determine request parameters based on media source
                use_multipart = False
                if media_bytes:
                    cached_fid = file_id_cache["id"]
                    if cached_fid:
                        endpoint, payload = _build_telegram_payload(uid, message, media_type, cached_fid, buttons)
                        send_files = None
                        send_data = None
                    else:
                        endpoint, send_files, send_data = _build_multipart_payload(
                            uid, message, media_type, media_bytes, media_filename, buttons
                        )
                        payload = None
                        use_multipart = True
                else:
                    endpoint, payload = _build_telegram_payload(uid, message, media_type, media_url, buttons)
                    send_files = None
                    send_data = None

                if payload is None and not use_multipart:
                    # No valid payload; classify as invalid without calling Telegram
                    new_status: str = "invalid"
                else:
                    api_url = f"https://api.telegram.org/bot{token}/{endpoint}"
                    new_status = "invalid"
                    while True:  # Retry loop – only retries on 429
                        try:
                            if use_multipart:
                                resp = await client.post(api_url, files=send_files, data=send_data)
                            else:
                                resp = await client.post(api_url, json=payload)
                            if resp.status_code == 429:
                                try:
                                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                                except Exception as e:
                                    log.warning("Failed to parse retry_after from 429 response: %s", e)
                                    retry_after = 5
                                log.warning("Bot %s 429 – sleeping %ss", bot_id, retry_after)
                                retry_until = asyncio.get_running_loop().time() + retry_after
                                await asyncio.sleep(retry_after)
                                continue  # Retry the same uid
                            rdata = resp.json()
                            if resp.status_code == 200 and rdata.get("ok"):
                                new_status = "active"
                                if use_multipart and not file_id_cache["id"]:
                                    try:
                                        result = rdata.get("result", {})
                                        fid = None
                                        if media_type == "image":
                                            photos = result.get("photo", [])
                                            if photos:
                                                fid = photos[-1]["file_id"]
                                        elif media_type == "video":
                                            fid = result.get("video", {}).get("file_id")
                                        if fid:
                                            file_id_cache["id"] = fid
                                    except Exception as fe:
                                        log.warning("Could not extract file_id: %s", fe)
                            elif resp.status_code == 403:
                                new_status = "blocked"
                            else:
                                log.warning(
                                    "Telegram API error for uid %s via bot %s: status=%s body=%s",
                                    uid, bot_id, resp.status_code, resp.text[:200],
                                )
                                new_status = "invalid"
                            break
                        except Exception as e:
                            log.warning("Failed to send message to %s via bot %s: %s", uid, bot_id, e)
                            new_status = "invalid"
                            break

                # Merge this bot's result into the per-uid best status
                async with stats_lock:
                    prev = uid_status_map[uid]
                    if (
                        new_status == "active"
                        or (new_status == "blocked" and prev not in ("active",))
                        or (prev is None and new_status == "invalid")
                    ):
                        uid_status_map[uid] = new_status
                    if new_status == "active":
                        bot_res[bot_id]["success"] += 1
                    else:
                        bot_res[bot_id]["fail"] += 1
                    uid_done_count[uid] += 1
                    all_done = uid_done_count[uid] >= num_active
                    final_status = uid_status_map[uid] if all_done else None
                    bot_res_snap = {k: dict(v) for k, v in bot_res.items()} if all_done else None

                if all_done:
                    resolved = final_status or "invalid"
                    try:
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE telegram_ids SET status=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                                (resolved, uid),
                            )
                            conn.commit()
                    except Exception as e:
                        log.warning("DB update failed for %s: %s", uid, e)
                    with _broadcast_lock:
                        _broadcast_status["done"] += 1
                        _broadcast_status[
                            resolved if resolved in ("active", "blocked", "invalid") else "error"
                        ] += 1
                        _broadcast_status["bot_results"] = bot_res_snap

                # Proactive rate limit: 25 msg/sec per bot (1 request per 0.04s)
                await asyncio.sleep(0.04)

    await asyncio.gather(*[bot_worker(bot) for bot in active_bots])

    with _broadcast_lock:
        _broadcast_status["running"] = False
        _broadcast_status["bot_results"] = {k: dict(v) for k, v in bot_res.items()}


async def _async_run_broadcast_all(
    bots: list, user_ids: list, message: str,
    media_type: str, media_url: str, buttons: list, camp_id,
    media_bytes: bytes = None, media_filename: str = None,
) -> None:
    """Async broadcast to active IDs with workload distributed across all bots.

    User IDs are split among bots via interleaved slicing so that N bots each
    handle ~1/N of the audience, achieving N × ~25 msg/sec total throughput.
    Per-bot 429 handling pauses only the affected bot; others continue.

    Supports optional file upload (media_bytes/media_filename).  When a file is
    provided, the first successful send uploads it via multipart and caches the
    returned file_id; all subsequent sends reuse that file_id via JSON to avoid
    redundant uploads.
    """
    active_bots = [b for b in bots if (b.get("token") or "").strip()]
    num_bots = len(active_bots)
    if num_bots == 0:
        with _broadcast_all_lock:
            _broadcast_all_status["running"] = False
        return

    bot_res: dict = {
        b.get("id", ""): {"name": b.get("name", "Bot"), "success": 0, "fail": 0}
        for b in bots
    }
    # Interleaved slicing: bot 0 → [0, N, 2N, …], bot 1 → [1, N+1, 2N+1, …], etc.
    bot_uid_slices = [user_ids[i::num_bots] for i in range(num_bots)]

    total_success = 0
    total_fail = 0
    done_count = 0
    stats_lock = asyncio.Lock()
    # Shared mutable cache for the Telegram file_id obtained from the first
    # successful multipart upload so subsequent sends reuse it efficiently.
    file_id_cache: dict = {"id": None}

    async def bot_worker(bot: dict, uid_slice: list) -> None:
        nonlocal total_success, total_fail, done_count
        token = (bot.get("token") or "").strip()
        bot_id = bot.get("id", "")
        if not token:
            return
        retry_until = 0.0  # Event-loop clock time after which this bot's 429 cooldown ends

        # 30s timeout accommodates multipart file uploads which are larger than plain JSON sends
        async with httpx.AsyncClient(timeout=30) as client:
            for uid in uid_slice:
                now = asyncio.get_running_loop().time()
                if retry_until > now:
                    await asyncio.sleep(retry_until - now)

                # Determine request parameters based on media source
                use_multipart = False
                if media_bytes:
                    cached_fid = file_id_cache["id"]
                    if cached_fid:
                        # Reuse the already-uploaded file_id as a URL parameter
                        endpoint, payload = _build_telegram_payload(uid, message, media_type, cached_fid, buttons)
                        send_files = None
                        send_data = None
                    else:
                        # First upload – use multipart
                        endpoint, send_files, send_data = _build_multipart_payload(
                            uid, message, media_type, media_bytes, media_filename, buttons
                        )
                        payload = None
                        use_multipart = True
                else:
                    endpoint, payload = _build_telegram_payload(uid, message, media_type, media_url, buttons)
                    send_files = None
                    send_data = None

                if payload is None and not use_multipart:
                    async with stats_lock:
                        bot_res[bot_id]["fail"] += 1
                        total_fail += 1
                        done_count += 1
                        snap = (done_count, total_success, total_fail, {k: dict(v) for k, v in bot_res.items()})
                    with _broadcast_all_lock:
                        _broadcast_all_status["done"] = snap[0]
                        _broadcast_all_status["success"] = snap[1]
                        _broadcast_all_status["fail"] = snap[2]
                        _broadcast_all_status["bot_results"] = snap[3]
                    await asyncio.sleep(0.04)
                    continue

                api_url = f"https://api.telegram.org/bot{token}/{endpoint}"
                uid_success = False
                while True:
                    try:
                        if use_multipart:
                            resp = await client.post(api_url, files=send_files, data=send_data)
                        else:
                            resp = await client.post(api_url, json=payload)
                        if resp.status_code == 429:
                            try:
                                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                            except Exception as e:
                                log.warning("Failed to parse retry_after from 429 response: %s", e)
                                retry_after = 5
                            log.warning("Bot %s 429 – sleeping %ss", bot_id, retry_after)
                            retry_until = asyncio.get_running_loop().time() + retry_after
                            await asyncio.sleep(retry_after)
                            continue
                        if resp.status_code == 200 and resp.json().get("ok"):
                            uid_success = True
                            # Cache the file_id returned by a multipart upload
                            if use_multipart and not file_id_cache["id"]:
                                try:
                                    result = resp.json().get("result", {})
                                    fid = None
                                    if media_type == "image":
                                        photos = result.get("photo", [])
                                        if photos:
                                            fid = photos[-1]["file_id"]
                                    elif media_type == "video":
                                        fid = result.get("video", {}).get("file_id")
                                    if fid:
                                        file_id_cache["id"] = fid
                                except Exception as fe:
                                    log.warning("Could not extract file_id from response: %s", fe)
                        elif resp.status_code == 403:
                            # User has blocked the bot – update the database
                            _mark_user_blocked(uid)
                        else:
                            log.warning(
                                "Telegram API error for uid %s via bot %s: status=%s body=%s",
                                uid, bot_id, resp.status_code, resp.text[:200],
                            )
                        break
                    except Exception as e:
                        log.warning("Failed to send message to %s via bot %s: %s", uid, bot_id, e)
                        break

                async with stats_lock:
                    if uid_success:
                        bot_res[bot_id]["success"] += 1
                        total_success += 1
                    else:
                        bot_res[bot_id]["fail"] += 1
                        total_fail += 1
                    done_count += 1
                    snap = (done_count, total_success, total_fail, {k: dict(v) for k, v in bot_res.items()})
                with _broadcast_all_lock:
                    _broadcast_all_status["done"] = snap[0]
                    _broadcast_all_status["success"] = snap[1]
                    _broadcast_all_status["fail"] = snap[2]
                    _broadcast_all_status["bot_results"] = snap[3]

                # Proactive rate limit: 25 msg/sec per bot (1 request per 0.04s)
                await asyncio.sleep(0.04)

    await asyncio.gather(
        *[bot_worker(bot, uid_slice) for bot, uid_slice in zip(active_bots, bot_uid_slices)]
    )

    with _broadcast_all_lock:
        _broadcast_all_status["running"] = False
        _broadcast_all_status["bot_results"] = {k: dict(v) for k, v in bot_res.items()}

    if camp_id is not None:
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE broadcast_logs SET success_count=?, fail_count=?, bot_results_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (total_success, total_fail, json.dumps(bot_res), camp_id),
                )
                conn.commit()
        except Exception as e:
            log.warning("broadcast_logs update failed: %s", e)


@app.route("/broadcast", methods=["POST"])
@login_required
def broadcast():
    """Send a broadcast message to all subscribers and active IDs via all configured bots.

    Supports optional rich media (image / video via URL or file upload) and
    inline keyboard buttons.  The send loop runs in a background thread so it
    never blocks Flask or causes a browser timeout when the subscriber list is
    large (1000+ users).

    Recipients: all subscribers (people who pressed /start) + all IDs with
    status='active' in telegram_ids, deduplicated.
    """
    import json as _json

    with _broadcast_all_lock:
        if _broadcast_all_status.get("running"):
            return jsonify({"ok": False, "error": "Đang có broadcast đang chạy, vui lòng đợi"}), 409

    with _broadcast_lock:
        if _broadcast_status.get("running"):
            return jsonify({"ok": False, "error": "Đang có broadcast (IDs) đang chạy, vui lòng đợi"}), 409

    bots_list = _get_all_active_bots()
    if not bots_list:
        return jsonify({"ok": False, "error": "Bot chưa được cấu hình"}), 500

    message = request.form.get("message", "").strip()
    media_type = request.form.get("media_type", "none").strip().lower()
    media_url = request.form.get("media_url", "").strip()
    buttons_raw = request.form.get("buttons_json", "").strip()

    # Handle optional file upload; infer media_type from extension when not set
    media_file = request.files.get("media_file")
    media_bytes: bytes | None = None
    media_filename: str | None = None
    if media_file and media_file.filename:
        media_bytes = media_file.read()
        media_filename = media_file.filename
        if media_type == "none":
            media_type = _infer_media_type_from_filename(media_filename)

    if not message and media_type == "none":
        return jsonify({"ok": False, "error": "Vui lòng nhập nội dung tin nhắn hoặc chọn media"}), 400
    if media_type in ("image", "video") and not media_url and not media_bytes:
        return jsonify({"ok": False, "error": "Vui lòng nhập URL media hoặc tải lên tệp"}), 400

    # Parse buttons
    buttons = []
    if buttons_raw:
        try:
            buttons = _json.loads(buttons_raw)
            if not isinstance(buttons, list):
                buttons = []
        except Exception:
            return jsonify({"ok": False, "error": "buttons_json không hợp lệ"}), 400

    # Recipients: all subscribers (pressed /start) + active IDs from telegram_ids
    sub_ids: set = db_get_all_subscriber_ids()
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM telegram_ids WHERE status = 'active'"
            ).fetchall()
        active_db_ids: set = {str(r["user_id"]) for r in rows}
    except Exception as e:
        log.error("broadcast DB read error: %s", e)
        active_db_ids = set()

    user_ids = list(sub_ids | active_db_ids)
    if not user_ids:
        return jsonify({"ok": False, "error": "Chưa có người đăng ký nào (chưa có ai nhấn /start)"}), 400

    bot_results_init = {b.get("id", ""): {"name": b.get("name", "Bot"), "success": 0, "fail": 0} for b in bots_list}

    with _broadcast_all_lock:
        _broadcast_all_status.update({
            "running": True, "total": len(user_ids), "done": 0,
            "success": 0, "fail": 0,
            "bot_results": bot_results_init,
        })

    # Log campaign start in DB
    campaign_id = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO broadcast_logs (campaign_type, message, media_type, media_url, buttons_json, total_ids) VALUES (?, ?, ?, ?, ?, ?)",
                ("subscribers", message, media_type if media_type != "none" else None,
                 media_url or None, buttons_raw or None, len(user_ids)),
            )
            campaign_id = cur.lastrowid
            conn.commit()
    except Exception as e:
        log.warning("broadcast_logs insert failed: %s", e)

    t = threading.Thread(
        target=lambda: asyncio.run(
            _async_run_broadcast_all(
                bots_list, user_ids, message, media_type, media_url, buttons, campaign_id,
                media_bytes=media_bytes, media_filename=media_filename,
            )
        ),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "total": len(user_ids), "bots_count": len(bots_list), "message": "Broadcast đã bắt đầu"})


@app.route("/broadcast/all/status", methods=["GET"])
@login_required
def broadcast_all_status():
    """Return current 'Broadcast All' task status."""
    return jsonify(_broadcast_all_status)


@app.route("/broadcast/all", methods=["POST"])
@login_required
def broadcast_all():
    """Broadcast to all active IDs in the database using all bots, with optional rich media and inline buttons."""
    import json as _json

    with _broadcast_all_lock:
        if _broadcast_all_status.get("running"):
            return jsonify({"ok": False, "error": "Đang có broadcast đang chạy, vui lòng đợi"}), 409

    # Also block if IDs broadcast is running
    with _broadcast_lock:
        if _broadcast_status.get("running"):
            return jsonify({"ok": False, "error": "Đang có broadcast (IDs) đang chạy, vui lòng đợi"}), 409

    bots_list = _get_all_active_bots()
    if not bots_list:
        return jsonify({"ok": False, "error": "Chưa cấu hình bot nào"}), 400

    message = request.form.get("message", "").strip()
    media_type = request.form.get("media_type", "none").strip().lower()
    media_url = request.form.get("media_url", "").strip()
    buttons_raw = request.form.get("buttons_json", "").strip()

    # Handle optional file upload; infer media_type from extension when not set
    media_file = request.files.get("media_file")
    media_bytes: bytes | None = None
    media_filename: str | None = None
    if media_file and media_file.filename:
        media_bytes = media_file.read()
        media_filename = media_file.filename
        if media_type == "none":
            media_type = _infer_media_type_from_filename(media_filename)

    if not message and media_type == "none":
        return jsonify({"ok": False, "error": "Vui lòng nhập nội dung tin nhắn hoặc chọn media"}), 400
    if media_type in ("image", "video") and not media_url and not media_bytes:
        return jsonify({"ok": False, "error": "Vui lòng nhập URL media hoặc tải lên tệp"}), 400

    # Parse buttons
    buttons = []
    if buttons_raw:
        try:
            buttons = _json.loads(buttons_raw)
            if not isinstance(buttons, list):
                buttons = []
        except Exception:
            return jsonify({"ok": False, "error": "buttons_json không hợp lệ"}), 400

    # Fetch active user IDs from the database (stored as TEXT, so already strings)
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM telegram_ids WHERE status = 'active'"
            ).fetchall()
        db_ids: set = {str(r["user_id"]) for r in rows}
    except Exception as e:
        log.error("broadcast_all DB read error: %s", e)
        return jsonify({"ok": False, "error": "Lỗi đọc database. Vui lòng thử lại."}), 500

    # Also include all subscriber IDs from the subscribers table
    sub_ids: set = db_get_all_subscriber_ids()

    # Merge: active DB IDs + subscriber IDs (deduplicated, all strings)
    user_ids = list(db_ids | sub_ids)

    if not user_ids:
        return jsonify({"ok": False, "error": "Không có ID nào để gửi. Hãy tải IDs lên hoặc chờ người dùng nhấn /start."}), 400

    bot_results_init = {b.get("id", ""): {"name": b.get("name", "Bot"), "success": 0, "fail": 0} for b in bots_list}

    with _broadcast_all_lock:
        _broadcast_all_status.update({
            "running": True, "total": len(user_ids), "done": 0,
            "success": 0, "fail": 0,
            "bot_results": bot_results_init,
        })

    # Log campaign start in DB
    campaign_id = None
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO broadcast_logs (campaign_type, message, media_type, media_url, buttons_json, total_ids) VALUES (?, ?, ?, ?, ?, ?)",
                ("all", message, media_type if media_type != "none" else None, media_url or None,
                 buttons_raw or None, len(user_ids)),
            )
            campaign_id = cur.lastrowid
            conn.commit()
    except Exception as e:
        log.warning("broadcast_logs insert failed: %s", e)

    t = threading.Thread(
        target=lambda: asyncio.run(
            _async_run_broadcast_all(
                bots_list, user_ids, message, media_type, media_url, buttons, campaign_id,
                media_bytes=media_bytes, media_filename=media_filename,
            )
        ),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "total": len(user_ids), "bots_count": len(bots_list), "message": "Broadcast All đã bắt đầu"})


@app.route("/settings/time", methods=["POST"])
@login_required
def settings_time():
    """Update auto-schedule hour and minute in the database (Vietnam time, UTC+7)."""
    try:
        hour = int(request.form.get("hour", 0))
        minute = int(request.form.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return jsonify({"ok": False, "error": "Giờ hoặc phút không hợp lệ"}), 400
        cfg = db_get_config()
        cfg["hour"] = hour
        cfg["minute"] = minute
        db_save_config(cfg)
        return jsonify({"ok": True, "hour": hour, "minute": minute})
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Dữ liệu không hợp lệ"}), 400


@app.route("/bots/add", methods=["POST"])
@owner_required
def add_bot():
    """Add a new bot to the database."""
    name = request.form.get("name", "").strip()
    token = request.form.get("token", "").strip()
    admin_id_str = request.form.get("admin_id", "").strip()
    if not name or not token:
        return jsonify({"ok": False, "error": "Tên và Token không được để trống"}), 400
    if admin_id_str and not admin_id_str.isdigit():
        return jsonify({"ok": False, "error": "Admin ID phải là số nguyên dương"}), 400
    admin_id_val = int(admin_id_str) if admin_id_str else None
    bots = db_get_bots()
    bots.append({
        "id": uuid.uuid4().hex,
        "name": name,
        "token": token,
        "admin_id": admin_id_val,
        "auto_responder": True,
    })
    db_save_bots(bots)
    _bot_manager.notify_change()
    return jsonify({"ok": True})


@app.route("/bots/<bot_id>/delete", methods=["POST"])
@owner_required
def delete_bot(bot_id):
    """Remove a bot from the database."""
    bots = db_get_bots()
    bots = [b for b in bots if b.get("id") != bot_id]
    db_save_bots(bots)
    _bot_manager.notify_change()
    return jsonify({"ok": True})


@app.route("/bots/<bot_id>/toggle_responder", methods=["POST"])
@owner_required
def toggle_bot_responder(bot_id):
    """Toggle the auto_responder flag for a bot."""
    bots = db_get_bots()
    updated = False
    new_state = None
    for bot in bots:
        if bot.get("id") == bot_id:
            bot["auto_responder"] = not bot.get("auto_responder", True)
            new_state = bot["auto_responder"]
            updated = True
            break
    if not updated:
        return jsonify({"ok": False, "error": "Bot không tồn tại"}), 404
    db_save_bots(bots)
    return jsonify({"ok": True, "auto_responder": new_state})


@app.route("/bots/health", methods=["GET"])
@owner_required
def bots_health():
    """Return health status and per-bot stats for all bots in the database."""
    bots_list = db_get_bots()
    # Load analytics from DB
    analytics_map = {a["bot_id"]: a for a in get_all_analytics()}
    result = []
    for bot in bots_list:
        token = (bot.get("token") or "").strip()
        bot_id = bot.get("id", "")
        status = "ERROR"
        if token:
            try:
                resp = httpx.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=5,
                )
                if resp.status_code == 200:
                    status = "LIVE"
                elif resp.status_code == 401:
                    status = "BAN"
                else:
                    status = "ERROR"
            except Exception:
                status = "ERROR"
        bot_subs = db_get_subscribers(bot_id)
        if not bot_subs:
            bot_subs = db_get_subscribers("global")
        a = analytics_map.get(bot_id, {})
        result.append({
            "id": bot_id,
            "status": status,
            "auto_responder": bot.get("auto_responder", True),
            "subs_count": len(bot_subs),
            "messages_sent": a.get("messages_sent", 0),
            "starts_count": a.get("starts_count", 0),
            "interactions_count": a.get("interactions_count", 0),
            "replies_count": a.get("replies_count", 0),
        })
    return jsonify(result)


@app.route("/analytics/data", methods=["GET"])
@login_required
def analytics_data():
    """Return aggregated analytics for all bots."""
    rows = get_all_analytics()
    totals = {
        "starts_count": sum(r["starts_count"] for r in rows),
        "messages_sent": sum(r["messages_sent"] for r in rows),
        "interactions_count": sum(r["interactions_count"] for r in rows),
        "replies_count": sum(r["replies_count"] for r in rows),
    }
    return jsonify({"ok": True, "totals": totals, "bots": rows})


@app.route("/bots/<bot_id>/token", methods=["GET"])
@owner_required
def get_bot_token(bot_id):
    """Return the full token of a bot (owner only)."""
    bots = db_get_bots()
    bot = next((b for b in bots if b.get("id") == bot_id), None)
    if not bot:
        return jsonify({"ok": False, "error": "Bot không tồn tại"}), 404
    return jsonify({"ok": True, "token": bot.get("token", "")})


def _get_all_active_bots() -> list:
    """Return list of all configured bots with valid tokens."""
    bots_list = db_get_bots()
    if not bots_list and BOT_TOKEN:
        bots_list = [{"id": "env_default", "name": "Default Bot", "token": BOT_TOKEN}]
    return [b for b in bots_list if (b.get("token") or "").strip()]


def _build_inline_keyboard(buttons: list) -> dict | None:
    """Return an inline_keyboard dict suitable for reply_markup, or None if no valid buttons."""
    inline_keyboard = []
    for btn in buttons:
        text = (btn.get("text") or "").strip()
        url = (btn.get("url") or "").strip()
        cb = (btn.get("callback_data") or "").strip()
        if text and (url or cb):
            if url:
                inline_keyboard.append([{"text": text, "url": url}])
            else:
                inline_keyboard.append([{"text": text, "callback_data": cb}])
    return {"inline_keyboard": inline_keyboard} if inline_keyboard else None


def _parse_chat_id(uid: str):
    """Convert a user-id string to an int when possible (required by Telegram API)."""
    uid_str = str(uid)
    if uid_str.isdigit() or (uid_str.startswith("-") and uid_str[1:].isdigit()):
        return int(uid_str)
    return uid


def _infer_media_type_from_filename(filename: str) -> str:
    """Return 'image', 'video', or 'none' based on the file extension."""
    if not filename:
        return "none"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("mp4", "mov", "avi", "mkv", "webm"):
        return "video"
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        return "image"
    return "none"


def _build_telegram_payload(uid: str, message: str, media_type: str, media_url: str, buttons: list) -> tuple:
    """Build (endpoint_suffix, payload_dict) for a Telegram API call with optional media and buttons.

    reply_markup is kept as a dict so that httpx serialises it correctly when
    using ``json=payload``.  Do NOT pre-serialise it to a JSON string here.
    """
    reply_markup = _build_inline_keyboard(buttons) if buttons else None
    chat_id = _parse_chat_id(uid)

    if media_type == "image" and media_url:
        payload: dict = {"chat_id": chat_id, "photo": media_url}
        if message:
            payload["caption"] = message
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return "sendPhoto", payload
    elif media_type == "video" and media_url:
        payload = {"chat_id": chat_id, "video": media_url}
        if message:
            payload["caption"] = message
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return "sendVideo", payload
    else:
        if not message:
            return "sendMessage", None  # Caller should validate before calling
        payload = {"chat_id": chat_id, "text": message}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return "sendMessage", payload


def _build_multipart_payload(
    uid: str, message: str, media_type: str,
    media_bytes: bytes, media_filename: str, buttons: list,
) -> tuple:
    """Build (endpoint, files_dict, form_data_dict) for a multipart Telegram file upload.

    reply_markup is JSON-serialised as a string here because multipart form
    fields are plain text values.
    """
    import json as _json

    chat_id = _parse_chat_id(uid)
    form_data: dict = {"chat_id": str(chat_id)}
    if message:
        form_data["caption"] = message

    reply_markup = _build_inline_keyboard(buttons) if buttons else None
    if reply_markup:
        form_data["reply_markup"] = _json.dumps(reply_markup)

    ext = media_filename.rsplit(".", 1)[-1].lower() if media_filename and "." in media_filename else ""
    if media_type == "video":
        field_name = "video"
        endpoint = "sendVideo"
        content_type = "video/mp4"
        if ext == "mov":
            content_type = "video/quicktime"
        elif ext == "avi":
            content_type = "video/x-msvideo"
        elif ext == "webm":
            content_type = "video/webm"
    else:
        field_name = "photo"
        endpoint = "sendPhoto"
        content_type = "image/jpeg"
        if ext == "png":
            content_type = "image/png"
        elif ext == "gif":
            content_type = "image/gif"
        elif ext == "webp":
            content_type = "image/webp"

    fname = media_filename or ("media.mp4" if media_type == "video" else "media.jpg")
    files = {field_name: (fname, media_bytes, content_type)}
    return endpoint, files, form_data


def _mark_user_blocked(uid: str) -> None:
    """Remove a blocked user from subscribers and update telegram_ids status to 'blocked'."""
    try:
        with get_db() as conn:
            try:
                uid_int = int(uid)
                conn.execute("DELETE FROM subscribers WHERE user_id=?", (uid_int,))
            except (ValueError, TypeError):
                pass
            conn.execute(
                "UPDATE telegram_ids SET status='blocked', updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (str(uid),),
            )
            conn.commit()
    except Exception as e:
        log.warning("_mark_user_blocked(%s) failed: %s", uid, e)


@app.route("/messages", methods=["GET"])
@login_required
def get_messages():
    """Return current message flow in node-based format."""
    return jsonify(get_messages_flow())


@app.route("/messages", methods=["POST"])
@login_required
def save_messages():
    """Save the complete message flow (dict of nodes)."""
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Dữ liệu không hợp lệ"}), 400
    clean_flow = {}
    for node_id, node in data.items():
        node_id = str(node_id).strip()
        if not node_id or not isinstance(node, dict):
            continue
        text = str(node.get("text", "")).strip()
        clean_buttons = _parse_node_buttons(node.get("buttons", []))
        clean_flow[node_id] = {"text": text, "buttons": clean_buttons}
    if "start" not in clean_flow:
        clean_flow["start"] = {"text": "Chào mừng bạn! 👋", "buttons": []}
    db_save_messages_flow(clean_flow)
    return jsonify({"ok": True})


def _parse_node_buttons(raw: list) -> list:
    """Validate and clean a list of node button dicts."""
    clean = []
    for btn in raw:
        if not isinstance(btn, dict):
            continue
        label = str(btn.get("label", "")).strip()
        btn_type = str(btn.get("type", "url")).strip()
        value = str(btn.get("value", "")).strip()
        if btn_type == "open_album_list" and label:
            # Always normalise value to "menu" for this special type
            clean.append({"label": label, "type": "open_album_list", "value": "menu"})
        elif label and value and btn_type in ("url", "node"):
            clean.append({"label": label, "type": btn_type, "value": value})
    return clean


@app.route("/messages/<node_id>", methods=["POST"])
@login_required
def save_message_node(node_id):
    """Save or update a single message node."""
    node_id = node_id.strip()
    if not node_id or not re.match(r'^[a-zA-Z0-9_-]+$', node_id):
        return jsonify({"ok": False, "error": "Node ID không hợp lệ (chỉ dùng chữ, số, _ -)"}), 400
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Dữ liệu không hợp lệ"}), 400
    text = str(data.get("text", "")).strip()
    clean_buttons = _parse_node_buttons(data.get("buttons", []))
    flow = get_messages_flow()
    flow[node_id] = {"text": text, "buttons": clean_buttons}
    db_save_messages_flow(flow)
    return jsonify({"ok": True})


@app.route("/messages/<node_id>/delete", methods=["POST"])
@login_required
def delete_message_node(node_id):
    """Delete a message node (cannot delete 'start')."""
    node_id = node_id.strip()
    if node_id == "start":
        return jsonify({"ok": False, "error": "Không thể xóa kịch bản 'start'"}), 400
    flow = get_messages_flow()
    flow.pop(node_id, None)
    db_save_messages_flow(flow)
    return jsonify({"ok": True})


@app.route("/slogans", methods=["GET"])
@login_required
def get_slogans():
    """Return current slogans configuration."""
    return jsonify(db_get_slogans())


@app.route("/slogans", methods=["POST"])
@login_required
def save_slogans():
    """Save slogans configuration."""
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Dữ liệu không hợp lệ"}), 400
    enabled = bool(data.get("enabled", True))
    raw_items = data.get("items", [])
    clean_items = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            try:
                delay = float(item.get("delay_after", 1))
                delay = max(0, min(30, delay))
            except (TypeError, ValueError):
                delay = 1.0
            if text:
                clean_items.append({"text": text, "delay_after": delay})
    db_save_slogans({"enabled": enabled, "items": clean_items})
    return jsonify({"ok": True})


# ===== ID MANAGEMENT =====

def _parse_ids_from_text(text: str) -> list:
    """Extract numeric Telegram IDs from plain text (one per line or comma-separated)."""
    ids = []
    for token in re.split(r"[\s,;]+", text):
        token = token.strip()
        if token.lstrip("-").isdigit():
            ids.append(token)
    return list(dict.fromkeys(ids))  # deduplicate preserving order


@app.route("/ids/upload", methods=["POST"])
@login_required
def upload_ids():
    """Upload a .txt or .csv file containing Telegram User IDs."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Không có file được chọn"}), 400
    fname = file.filename.lower()
    if not (fname.endswith(".txt") or fname.endswith(".csv")):
        return jsonify({"ok": False, "error": "Chỉ chấp nhận file .txt hoặc .csv"}), 400
    try:
        content = file.read().decode("utf-8", errors="replace")
    except Exception as e:
        return jsonify({"ok": False, "error": f"Không đọc được file: {e}"}), 400
    ids = _parse_ids_from_text(content)
    if not ids:
        return jsonify({"ok": False, "error": "Không tìm thấy ID hợp lệ trong file"}), 400
    inserted = 0
    skipped = 0
    try:
        with get_db() as conn:
            for uid in ids:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO telegram_ids (user_id, status) VALUES (?, 'unknown')",
                        (uid,),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        inserted += 1
                    else:
                        skipped += 1
                except Exception:
                    skipped += 1
            conn.commit()
    except Exception as e:
        log.error(f"ID upload DB error: {e}")
        return jsonify({"ok": False, "error": "Lỗi cơ sở dữ liệu"}), 500
    return jsonify({"ok": True, "inserted": inserted, "skipped": skipped, "total_parsed": len(ids)})


@app.route("/ids", methods=["GET"])
@login_required
def list_ids():
    """Return paginated list of uploaded IDs."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(10, int(request.args.get("per_page", 50))))
    status_filter = request.args.get("status", "")
    offset = (page - 1) * per_page
    try:
        with get_db() as conn:
            if status_filter:
                total = conn.execute(
                    "SELECT COUNT(*) FROM telegram_ids WHERE status=?", (status_filter,)
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT user_id, status, created_at, updated_at FROM telegram_ids "
                    "WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (status_filter, per_page, offset),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) FROM telegram_ids").fetchone()[0]
                rows = conn.execute(
                    "SELECT user_id, status, created_at, updated_at FROM telegram_ids "
                    "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (per_page, offset),
                ).fetchall()
        items = [dict(r) for r in rows]
        return jsonify({"ok": True, "total": total, "page": page, "per_page": per_page, "items": items})
    except Exception as e:
        log.error(f"list_ids error: {e}")
        return jsonify({"ok": False, "error": "Lỗi cơ sở dữ liệu"}), 500


@app.route("/ids/stats", methods=["GET"])
@login_required
def ids_stats():
    """Return count of IDs grouped by status."""
    stats = {"total": 0, "unknown": 0, "active": 0, "blocked": 0, "invalid": 0}
    try:
        with get_db() as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM telegram_ids GROUP BY status"
            ):
                stats[row["status"]] = row["cnt"]
                stats["total"] += row["cnt"]
    except Exception as e:
        log.error(f"ids_stats error: {e}")
    return jsonify(stats)


@app.route("/ids/clear", methods=["POST"])
@owner_required
def clear_ids():
    """Delete all uploaded IDs (owner only)."""
    status_filter = request.form.get("status", "").strip()
    try:
        with get_db() as conn:
            if status_filter:
                conn.execute("DELETE FROM telegram_ids WHERE status=?", (status_filter,))
            else:
                conn.execute("DELETE FROM telegram_ids")
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"clear_ids error: {e}")
        return jsonify({"ok": False, "error": "Lỗi cơ sở dữ liệu"}), 500


@app.route("/ids/broadcast/status", methods=["GET"])
@login_required
def ids_broadcast_status():
    """Return current broadcast task status."""
    return jsonify(_broadcast_status)


@app.route("/ids/broadcast", methods=["POST"])
@login_required
def ids_broadcast():
    """Broadcast a message to all uploaded IDs using ALL configured bots, classifying status via Telegram API errors.

    Supports optional rich media (image / video via URL or file upload) and inline keyboard buttons alongside the text message.
    """
    import json as _json

    with _broadcast_lock:
        if _broadcast_status.get("running"):
            return jsonify({"ok": False, "error": "Đang có broadcast đang chạy, vui lòng đợi"}), 409

    # Also block if Broadcast All is running
    with _broadcast_all_lock:
        if _broadcast_all_status.get("running"):
            return jsonify({"ok": False, "error": "Đang có Broadcast All đang chạy, vui lòng đợi"}), 409

    bots_list = _get_all_active_bots()
    if not bots_list:
        return jsonify({"ok": False, "error": "Chưa cấu hình bot nào"}), 400

    message = request.form.get("message", "").strip()
    media_type = request.form.get("media_type", "none").strip().lower()
    media_url = request.form.get("media_url", "").strip()
    buttons_raw = request.form.get("buttons_json", "").strip()

    # Handle optional file upload; infer media_type from extension when not set
    media_file = request.files.get("media_file")
    media_bytes: bytes | None = None
    media_filename: str | None = None
    if media_file and media_file.filename:
        media_bytes = media_file.read()
        media_filename = media_file.filename
        if media_type == "none":
            media_type = _infer_media_type_from_filename(media_filename)

    if not message and media_type == "none":
        return jsonify({"ok": False, "error": "Tin nhắn không được để trống"}), 400
    if media_type in ("image", "video") and not media_url and not media_bytes:
        return jsonify({"ok": False, "error": "Vui lòng nhập URL media hoặc tải lên tệp"}), 400

    buttons = []
    if buttons_raw:
        try:
            buttons = _json.loads(buttons_raw)
            if not isinstance(buttons, list):
                buttons = []
        except Exception:
            return jsonify({"ok": False, "error": "buttons_json không hợp lệ"}), 400

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM telegram_ids WHERE status IN ('unknown', 'active')"
            ).fetchall()
        user_ids = [r["user_id"] for r in rows]
    except Exception as e:
        log.error("ids_broadcast DB read error: %s", e)
        return jsonify({"ok": False, "error": "Lỗi đọc database. Vui lòng thử lại."}), 500

    if not user_ids:
        return jsonify({"ok": False, "error": "Không có ID nào để gửi"}), 400

    bot_results_init = {b.get("id", ""): {"name": b.get("name", "Bot"), "success": 0, "fail": 0} for b in bots_list}

    with _broadcast_lock:
        _broadcast_status.update({
            "running": True, "total": len(user_ids), "done": 0,
            "active": 0, "blocked": 0, "invalid": 0, "error": 0,
            "bot_results": bot_results_init,
        })

    t = threading.Thread(
        target=lambda: asyncio.run(
            _async_run_ids_broadcast(
                bots_list, message, user_ids, media_type, media_url, buttons,
                media_bytes=media_bytes, media_filename=media_filename,
            )
        ),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "total": len(user_ids), "bots_count": len(bots_list), "message": "Broadcast đã bắt đầu"})


# ===== BOT HANDLERS =====

if BOT_TOKEN or db_get_bots():
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

    # ---- Flow keyboard builder ----

    def _build_flow_keyboard(buttons: list) -> list:
        """Build InlineKeyboardMarkup rows from a node's button list."""
        keyboard = []
        for btn in buttons:
            label = btn.get("label", "")
            btn_type = btn.get("type", "url")
            value = btn.get("value", "")
            if not label:
                continue
            if btn_type == "url" and value:
                keyboard.append([InlineKeyboardButton(label, url=value)])
            elif btn_type == "open_album_list":
                keyboard.append([InlineKeyboardButton(label, callback_data="menu")])
            elif btn_type == "node" and value:
                keyboard.append([InlineKeyboardButton(label, callback_data=f"flow_{value}")])
        return keyboard

    # ---- Shared handlers (no per-bot subscriber state needed) ----

    async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        albums = db_get_albums()
        log.info("menu handler: loaded albums: %s", list(albums.keys()))
        buttons = []
        for key, val in sorted(albums.items()):
            title = val.get("title", key) if isinstance(val, dict) else key
            buttons.append([InlineKeyboardButton(f"🔥 {title}", callback_data=key)])
        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])

        await query.edit_message_text("CHỌN COMBO LlNK HOT🔥", reply_markup=InlineKeyboardMarkup(buttons))

    async def send_album(chat_id, bot, album):
        """Send all posts in an album (supports images, videos, and legacy photos[] format).

        HLS videos (.m3u8) are sent as inline Web App buttons so the user can
        stream them through the built-in player page instead of downloading a
        raw video file.
        """
        base_url = os.environ.get("APP_BASE_URL", os.environ.get("DOMAIN", "")).rstrip("/")

        if "posts" in album:
            groups = [(post.get("photos", []), post.get("caption", "")) for post in album["posts"]]
        else:
            groups = [(album.get("photos", []), "")]

        for photos_list, group_caption in groups:
            media = []
            open_files = []
            hls_buttons = []
            try:
                for i, p in enumerate(photos_list):
                    url = p["url"]
                    item_type = p.get("type", "image")

                    # HLS video: extract file_id from the m3u8 URL and send as Web App button
                    if item_type == "video" and url.endswith("/index.m3u8"):
                        # URL pattern: /static/uploads/videos/<file_id>/index.m3u8
                        if not base_url:
                            log.warning("APP_BASE_URL (or DOMAIN) not set – cannot create HLS stream button; skipping video")
                            continue
                        parts = url.rstrip("/").split("/")
                        if len(parts) < 2:
                            log.warning(f"Unexpected HLS URL format, skipping: {url}")
                            continue
                        file_id = parts[-2]
                        stream_url = f"{base_url}/stream/{file_id}"
                        label = f"▶️ Xem video {len(hls_buttons) + 1}"
                        hls_buttons.append([InlineKeyboardButton(label, url=stream_url)])
                        continue

                    if url.startswith("/static/uploads/"):
                        # Use the app directory to build an absolute path so the file
                        # can be opened regardless of the process's current working directory.
                        abs_path = os.path.join(_APP_DIR, url.lstrip("/"))
                        f = open(abs_path, "rb")
                        open_files.append(f)
                        media_src = f
                    else:
                        media_src = url
                    caption = group_caption if i == 0 else ""
                    if item_type == "video":
                        media.append(InputMediaVideo(media_src, caption=caption))
                    else:
                        media.append(InputMediaPhoto(media_src, caption=caption))

                if media:
                    await bot.send_media_group(chat_id, media)
                if hls_buttons:
                    caption_text = group_caption or "🎬 Nhấn để xem video"
                    await bot.send_message(
                        chat_id,
                        caption_text,
                        reply_markup=InlineKeyboardMarkup(hls_buttons),
                    )
            finally:
                for f in open_files:
                    f.close()

    async def album_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "back":
            flow = get_messages_flow()
            start_node = flow.get("start", {"text": "Chào mừng bạn! 👋", "buttons": []})
            back_text = start_node.get("text", "Chào mừng bạn! 👋") or "Chào mừng bạn! 👋"
            keyboard = _build_flow_keyboard(start_node.get("buttons", []))
            if not keyboard:
                keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
            await query.edit_message_text(
                back_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        albums = db_get_albums()
        album = albums.get(query.data)
        chat_id = query.message.chat.id
        await query.delete_message()

        if album:
            await send_album(chat_id, context.bot, album)
            # Send end-of-album closing message
            flow = get_messages_flow()
            album_end = flow.get("album_end", {})
            end_text = (album_end.get("text") or _DEFAULT_ALBUM_END_NODE["text"]).strip()
            keyboard = _build_flow_keyboard(album_end.get("buttons", []))
            if not keyboard:
                keyboard = [[InlineKeyboardButton("🔙 Quay lại danh sách Album", callback_data="menu")]]
            await context.bot.send_message(
                chat_id,
                end_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    async def flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle flow navigation callbacks (callback_data starting with 'flow_')."""
        query = update.callback_query
        await query.answer()
        node_id = query.data[5:]  # strip "flow_" prefix
        flow = get_messages_flow()
        node = flow.get(node_id)
        if not node:
            await query.answer("⚠️ Không tìm thấy tin nhắn này.", show_alert=True)
            return
        text = node.get("text", "") or "..."
        keyboard = _build_flow_keyboard(node.get("buttons", []))
        try:
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
            )
        except Exception:
            pass

    # ---- Per-bot application factory ----

    def _build_ptb_app(token: str, bot_admin_id, bot_cfg_id: str = "env_default"):
        """Build and return a PTB Application for one bot token, with admin handlers
        and subscriber state bound to bot_cfg_id via closures."""
        from telegram.ext import MessageHandler, filters as tg_filters

        _last_sent = [None]  # mutable list so the nested coroutine can update it

        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_chat.id
            banned = db_get_banned()
            if user_id in banned:
                await update.message.reply_text("⛔ Bạn đã bị cấm sử dụng bot này.")
                return

            # Track starts analytics (always, regardless of auto_responder)
            increment_bot_stat(bot_cfg_id, "starts_count")

            db_add_subscriber(bot_cfg_id, user_id)

            # Check auto_responder flag dynamically (can be toggled without restart)
            bots_cfg = db_get_bots()
            bot_entry = next((b for b in bots_cfg if b.get("id") == bot_cfg_id), None)
            auto_responder = bot_entry.get("auto_responder", True) if bot_entry else True
            if not auto_responder:
                return

            # Send slogans sequentially if enabled
            slogans_cfg = db_get_slogans()
            if slogans_cfg.get("enabled") and slogans_cfg.get("items"):
                slogan_msgs = []
                for item in slogans_cfg["items"]:
                    text = str(item.get("text", "")).strip()
                    try:
                        delay = float(item.get("delay_after", 1))
                        delay = max(0, min(30, delay))
                    except (TypeError, ValueError):
                        delay = 1.0
                    if text:
                        msg = await update.message.reply_text(text)
                        slogan_msgs.append(msg)
                        await asyncio.sleep(delay)
                # Delete all slogan messages
                for m in slogan_msgs:
                    try:
                        await m.delete()
                    except Exception:
                        pass

            # Load message flow in real-time
            flow = get_messages_flow()
            start_node = flow.get("start", {"text": "Chào mừng bạn! 👋", "buttons": []})
            start_text = start_node.get("text", "Chào mừng bạn! 👋") or "Chào mừng bạn! 👋"
            keyboard = _build_flow_keyboard(start_node.get("buttons", []))
            if not keyboard:
                keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]

            await update.message.reply_text(
                start_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        async def track_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Count incoming text messages (user replies) for analytics."""
            increment_bot_stat(bot_cfg_id, "replies_count")

        async def track_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Track callback query interactions before delegating to real handlers."""
            # Only count once; actual handling is done by other handlers (menu/flow/album_click)
            increment_bot_stat(bot_cfg_id, "interactions_count")

        async def menu_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await track_interaction(update, context)
            await menu(update, context)

        async def flow_handler_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await track_interaction(update, context)
            await flow_handler(update, context)

        async def album_click_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await track_interaction(update, context)
            await album_click(update, context)

        async def set_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            try:
                time_str = update.message.text.split("_")[1]
                h, m = map(int, time_str.split(":"))
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    await update.message.reply_text("Giờ/phút không hợp lệ (0-23 / 0-59)")
                    return
                db_save_config({"hour": h, "minute": m})
                await update.message.reply_text(f"✅ Đã set giờ: {h:02d}:{m:02d} (Giờ VN)")
            except Exception:
                await update.message.reply_text("Sai format. Dùng: /settudongguilink_HH:MM")

        async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            bot_subs = db_get_subscribers(bot_cfg_id)
            albums = db_get_albums()
            total = total_posts(albums)
            a_rows = get_all_analytics()
            a_data = next((r for r in a_rows if r["bot_id"] == bot_cfg_id), {})
            await update.message.reply_text(
                f"📊 Thống kê Bot:\n"
                f"👥 Người đăng ký: {len(bot_subs)}\n"
                f"📁 Albums: {len(albums)}\n"
                f"📝 Bài viết: {total}\n"
                f"▶️ Lượt /start: {a_data.get('starts_count', 0)}\n"
                f"📤 Tin đã gửi: {a_data.get('messages_sent', 0)}\n"
                f"🖱️ Tương tác nút: {a_data.get('interactions_count', 0)}\n"
                f"💬 Phản hồi: {a_data.get('replies_count', 0)}"
            )

        async def sendall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            message_text = " ".join(context.args) if context.args else ""
            if not message_text:
                await update.message.reply_text("Cú pháp: /sendall <nội dung tin nhắn>")
                return
            # Use all subscriber IDs across all bots so no subscriber is missed
            all_sub_ids = db_get_all_subscriber_ids()
            if not all_sub_ids:
                await update.message.reply_text("Chưa có người đăng ký nào.")
                return
            success, fail = 0, 0
            for user_id in all_sub_ids:
                try:
                    chat_id = int(user_id) if str(user_id).lstrip("-").isdigit() else user_id
                    await context.bot.send_message(chat_id, message_text)
                    success += 1
                except Exception:
                    fail += 1
            increment_messages_sent(bot_cfg_id, success)
            await update.message.reply_text(
                f"📢 Đã gửi thông báo!\n✅ Thành công: {success}\n❌ Thất bại: {fail}"
            )

        async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            if not context.args or not context.args[0].lstrip("-").isdigit():
                await update.message.reply_text("Cú pháp: /ban <user_id>")
                return
            target_id = int(context.args[0])
            db_remove_subscriber(bot_cfg_id, target_id)
            db_add_ban(target_id)
            await update.message.reply_text(f"✅ Đã cấm người dùng {target_id}.")

        async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
            now_vn = datetime.now(timezone.utc) + timedelta(hours=7)
            cfg = db_get_config()
            if now_vn.hour == cfg["hour"] and now_vn.minute == cfg["minute"]:
                key = f"{now_vn.hour}:{now_vn.minute}"
                if _last_sent[0] != key:
                    _last_sent[0] = key
                    albums = db_get_albums()
                    if not albums:
                        return
                    latest = sorted(albums.keys())[-1]
                    bot_subs = db_get_subscribers(bot_cfg_id)
                    success, fail = 0, 0
                    for u in bot_subs:
                        try:
                            await send_album(u, context.bot, albums[latest])
                            success += 1
                        except Exception:
                            fail += 1
                    increment_messages_sent(bot_cfg_id, success)
                    if bot_admin_id:
                        await context.bot.send_message(
                            bot_admin_id,
                            f"📊 Report\nUsers: {len(bot_subs)}\nOK: {success}\nFail: {fail}",
                        )

        async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
            try:
                url = run_daily_backup()
                if bot_admin_id:
                    await context.bot.send_message(bot_admin_id, f"✅ Backup completed!\n🔗 {url}")
            except Exception as e:
                log.error(f"Daily backup failed: {e}")
                if bot_admin_id:
                    await context.bot.send_message(bot_admin_id, f"❌ Backup failed: {e}")

        ptb_app = Application.builder().token(token).build()
        ptb_app.add_handler(CommandHandler("start", start))
        ptb_app.add_handler(CommandHandler("settudongguilink", set_time_cmd))
        ptb_app.add_handler(CommandHandler("stats", stats_cmd))
        ptb_app.add_handler(CommandHandler("sendall", sendall_cmd))
        ptb_app.add_handler(CommandHandler("ban", ban_cmd))
        ptb_app.add_handler(CallbackQueryHandler(menu_tracked, pattern="^menu$"))
        ptb_app.add_handler(CallbackQueryHandler(flow_handler_tracked, pattern="^flow_"))
        ptb_app.add_handler(CallbackQueryHandler(album_click_tracked))
        # Track non-command text messages as user replies
        ptb_app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, track_reply))
        ptb_app.job_queue.run_repeating(scheduler_job, interval=30, first=1)
        ptb_app.job_queue.run_daily(daily_backup_job, time=dt_time(1, 0, 0, tzinfo=timezone.utc))
        return ptb_app

    class _BotManager:
        """Dynamically manages PTB bot instances: starts new bots and stops removed bots
        by periodically reloading bots.json without requiring a server restart."""

        def __init__(self):
            self._running_apps: dict = {}  # {bot_id: ptb_app}
            self._loop: asyncio.AbstractEventLoop | None = None
            self._reload_event: asyncio.Event | None = None

        def notify_change(self):
            """Signal that bots.json has changed; triggers an immediate reload cycle."""
            if self._loop and self._reload_event and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._reload_event.set)

        async def _reload(self):
            """Sync running PTB apps with the current bots DB table."""
            log.info("BotManager._reload() called – reading bots from DB")
            bots_cfg = db_get_bots()
            log.info("BotManager._reload() – DB contains %d bot entry/entries", len(bots_cfg))
            if not bots_cfg and BOT_TOKEN:
                log.info(
                    "BotManager._reload() – bots DB is empty; falling back to "
                    "BOT_TOKEN env var (token length=%d)", len(BOT_TOKEN)
                )
                bots_cfg = [{"id": "env_default", "name": "Default Bot",
                              "token": BOT_TOKEN, "admin_id": ADMIN_ID}]
            elif not bots_cfg and not BOT_TOKEN:
                log.warning(
                    "BotManager._reload() – bots DB is empty AND BOT_TOKEN is unset; "
                    "no bots to start. Set TOKEN/BOT_TOKEN in .env or add a bot via the web UI."
                )

            current_ids = {
                b.get("id") for b in bots_cfg if (b.get("token") or "").strip()
            }
            running_ids = set(self._running_apps.keys())
            log.info(
                "BotManager._reload() – current_ids=%s running_ids=%s",
                current_ids, running_ids,
            )

            # Stop apps for bots that have been removed
            for bot_id in list(running_ids - current_ids):
                ptb = self._running_apps.pop(bot_id)
                try:
                    await ptb.updater.stop()
                    await ptb.stop()
                    await ptb.shutdown()
                    log.info("Bot %s stopped and removed", bot_id)
                except Exception as exc:
                    log.warning("Error stopping bot %s: %s", bot_id, exc)

            # Start apps for newly added bots
            for cfg_entry in bots_cfg:
                bot_id = cfg_entry.get("id", "env_default")
                token = (cfg_entry.get("token") or "").strip()
                if not token:
                    log.warning(
                        "BotManager._reload() – skipping bot id=%s (name=%s): token is empty",
                        bot_id, cfg_entry.get("name", "?"),
                    )
                    continue
                if bot_id in self._running_apps:
                    log.debug("BotManager._reload() – bot %s already running, skipping", bot_id)
                    continue
                log.info(
                    "BotManager._reload() – starting bot id=%s name=%s (token length=%d)",
                    bot_id, cfg_entry.get("name", "?"), len(token),
                )
                try:
                    _aid_raw = cfg_entry.get("admin_id")
                    _aid = int(_aid_raw) if _aid_raw and str(_aid_raw).isdigit() else ADMIN_ID
                    ptb = _build_ptb_app(token, _aid, bot_id)
                    log.info("BotManager._reload() – calling initialize() for bot %s", bot_id)
                    await ptb.initialize()
                    log.info("BotManager._reload() – calling start() for bot %s", bot_id)
                    await ptb.start()
                    log.info("BotManager._reload() – calling start_polling() for bot %s", bot_id)
                    await ptb.updater.start_polling(
                        allowed_updates=["message", "callback_query"],
                        drop_pending_updates=True,
                    )
                    self._running_apps[bot_id] = ptb
                    log.info("✅ Bot %s (%s) started polling", bot_id, cfg_entry.get("name", "?"))
                except Exception as exc:
                    log.error(
                        "Failed to start bot %s (%s): %s",
                        bot_id, cfg_entry.get("name", "?"), exc,
                        exc_info=True,
                    )

        async def _manage(self):
            """Main async management loop: reload on signal or every 30 s."""
            self._reload_event = asyncio.Event()
            await self._reload()
            while True:
                try:
                    await asyncio.wait_for(self._reload_event.wait(), timeout=30.0)
                    self._reload_event.clear()
                except asyncio.TimeoutError:
                    pass
                await self._reload()

        def start_in_thread(self):
            """Run the management loop in a dedicated daemon thread."""
            def _run():
                log.info("BotManager thread started (PID %s)", os.getpid())
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                try:
                    self._loop.run_until_complete(self._manage())
                except Exception as exc:
                    log.error("BotManager loop error: %s", exc, exc_info=True)
                finally:
                    log.warning("BotManager thread exiting (PID %s)", os.getpid())
            t = threading.Thread(target=_run, daemon=True, name="bot-manager")
            t.start()
            log.info("BotManager daemon thread launched (thread id=%s)", t.ident)
            return t

    _bot_manager = _BotManager()

    # Auto-start the bot manager background thread when Flask/Gunicorn loads the module.
    #
    # Design notes:
    # 1. Multi-worker safety: A non-blocking exclusive file lock (fcntl.flock) ensures
    #    only ONE Gunicorn worker process starts the bot polling thread, even when
    #    Gunicorn is launched with -w 2+ workers.
    # 2. Werkzeug dev-server: When running flask with debug=True the reloader spawns a
    #    child process with WERKZEUG_RUN_MAIN="true".  Both parent and child execute this
    #    module-level code, but the file lock guarantees only whichever loads first
    #    actually starts the bot — the other gets IOError and skips safely.
    # 3. The lock file descriptor (_bot_lock_fd) is intentionally stored as a module-level
    #    variable so it is NOT garbage-collected.  The lock is held for the entire process
    #    lifetime; releasing it (by GC or close()) would free the lock and allow a second
    #    process to start a competing bot instance.
    _bot_lock_fd = None  # module-level reference to keep FD alive
    try:
        import fcntl as _fcntl

        _bot_lock_path = os.path.join(tempfile.gettempdir(), "telegram_bot_manager.lock")
        log.info("Bot manager: attempting to acquire lock at %s (worker PID %s)", _bot_lock_path, os.getpid())
        _bot_lock_fd = open(_bot_lock_path, "w")  # noqa: WPS515 – intentionally kept open
        _fcntl.flock(_bot_lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        log.info("Bot manager: lock acquired – starting background thread (worker PID %s)", os.getpid())
        _bot_manager.start_in_thread()
        log.info("Bot manager started in background thread (worker PID %s)", os.getpid())
    except (IOError, OSError):
        log.info(
            "Bot manager lock held by another worker (PID %s) – "
            "bot polling thread not started in this worker",
            os.getpid(),
        )
        if _bot_lock_fd is not None:
            _bot_lock_fd.close()
    except Exception as _bot_start_err:
        log.error("Failed to auto-start bot manager: %s", _bot_start_err, exc_info=True)
        if _bot_lock_fd is not None:
            _bot_lock_fd.close()

else:
    log.warning(
        "No bot token configured – bot disabled, Flask only. "
        "To enable the bot: add TOKEN=<your-token> to the .env file, "
        "or add a bot via the web admin panel (which writes bots.json). "
        "Current env keys checked: TELEGRAM_BOT_TOKEN, TOKEN, BOT_TOKEN."
    )


def run_bot_thread():
    """Entry point called by run_bot.py — runs the dynamic BotManager loop.

    If no bot token is configured and the bots DB table is empty, this is a no-op.
    Otherwise it acquires the exclusive bot-manager lock (blocking) and then
    starts the BotManager loop.  This ensures that if Gunicorn's embedded bot
    thread is already running (and holds the lock), run_bot_thread() waits
    until that thread exits before taking over — providing a seamless handoff
    rather than a conflicting dual-polling situation.
    """
    bots_cfg = db_get_bots()
    if not bots_cfg and not BOT_TOKEN:
        log.warning("run_bot_thread: no bots configured – exiting")
        return

    try:
        # _BotManager is defined inside the 'if BOT_TOKEN or ...' block above;
        # if that block ran, _BotManager is available in the module globals.
        BotManagerCls = globals()["_BotManager"]
    except KeyError:
        log.warning("run_bot_thread: _BotManager not available – no bot token configured")
        return

    # Acquire the exclusive bot-manager lock (BLOCKING).  If Gunicorn's
    # embedded bot thread currently holds the lock, we wait here until it
    # releases (e.g. the Gunicorn worker dies), then start the bot ourselves.
    #
    # _bot_lock_fd is stored as a local (not closed) so the lock is held for the
    # entire duration of run_bot_thread().  It is released automatically when
    # this function returns and the local variable is garbage-collected.
    try:
        import fcntl as _fcntl
        _bot_lock_path = os.path.join(tempfile.gettempdir(), "telegram_bot_manager.lock")
        _bot_lock_fd = open(_bot_lock_path, "w")  # noqa: WPS515 – held open for lock lifetime
        log.info("run_bot_thread: waiting for bot manager lock (PID %s)…", os.getpid())
        _fcntl.flock(_bot_lock_fd, _fcntl.LOCK_EX)  # blocking – waits until the lock is free
        log.info("run_bot_thread: acquired bot lock (PID %s), starting bot", os.getpid())
    except Exception as _lock_err:
        log.error("run_bot_thread: failed to acquire bot lock: %s", _lock_err)
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    manager = BotManagerCls()
    try:
        loop.run_until_complete(manager._manage())
    except Exception as e:
        log.error("Bot thread error: %s", e)
        raise


# ===== APSCHEDULER — automated daily backup at 0:01 AM Vietnam Time =====
try:
    from apscheduler.schedulers.background import BackgroundScheduler as _BGScheduler
    import pytz as _pytz

    def _scheduled_daily_backup():
        """Called by APScheduler to run the daily backup."""
        log.info("APScheduler: starting daily backup...")
        try:
            url = run_daily_backup()
            log.info(f"APScheduler: daily backup complete → {url}")
        except Exception as e:
            log.error(f"APScheduler: daily backup failed: {e}")

    # Guard against Werkzeug debug reloader: in debug mode the parent process
    # (reloader) also imports app.py; only start the scheduler in the child
    # worker process (where WERKZEUG_RUN_MAIN == "true") or in production
    # (where WERKZEUG_RUN_MAIN is unset).
    if os.environ.get("WERKZEUG_RUN_MAIN") != "false":
        _vn_tz = _pytz.timezone("Asia/Ho_Chi_Minh")
        _scheduler = _BGScheduler(timezone=_vn_tz)
        _scheduler.add_job(_scheduled_daily_backup, "cron", hour=0, minute=1,
                           id="daily_backup", replace_existing=True)
        _scheduler.start()
        log.info("APScheduler started – daily backup scheduled at 0:01 AM Vietnam Time")
except ImportError:
    log.warning("apscheduler not installed – automated backup disabled. Run: pip install apscheduler pytz")
except Exception as _aps_err:
    log.error(f"Failed to start APScheduler: {_aps_err}")


# ===== MAIN =====
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
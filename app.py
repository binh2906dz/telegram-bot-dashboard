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

import httpx
import cloudinary
import cloudinary.uploader
import cloudinary.api

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", os.environ.get("TOKEN", ""))
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

ALBUMS_FILE = "albums.json"
SUBS_FILE = "subscribers.json"
CONFIG_FILE = "config.json"
BANNED_FILE = "banned.json"
BOTS_FILE = "bots.json"
MESSAGES_FILE = "messages.json"
STATS_FILE = "stats.json"
SLOGANS_FILE = "slogans.json"
DB_FILE = "data.db"

# ===== CLOUDINARY CONFIG =====
# Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET
# as environment variables in your deployment (e.g. Railway Variables).
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    secure=True,
)
CLOUDINARY_BACKUP_FOLDER = "backups"


_cache: dict = {}  # {file: {"mtime": float, "data": ...}}


def load_json(file, default):
    try:
        mtime = os.path.getmtime(file)
        entry = _cache.get(file)
        if entry and entry["mtime"] == mtime:
            return entry["data"]
        with open(file) as f:
            data = json.load(f)
        # Re-check mtime after reading so the cached value reflects the on-disk state
        _cache[file] = {"mtime": os.path.getmtime(file), "data": data}
        return data
    except Exception:
        return default


def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        _cache[file] = {"mtime": os.path.getmtime(file), "data": data}
    except Exception:
        _cache.pop(file, None)


# ===== SQLITE DATABASE (for ID management, future Turso migration) =====

def get_db() -> sqlite3.Connection:
    """Return a new SQLite connection with row_factory set."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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
        conn.commit()


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


def total_posts(albums):
    return sum(len(a.get("posts", [])) for a in albums.values() if isinstance(a, dict))


def subs_file(bot_id: str) -> str:
    """Return per-bot subscriber file path, falling back to global file."""
    if not bot_id or bot_id == "env_default":
        return SUBS_FILE
    return f"subs_{bot_id}.json"


def increment_messages_sent(bot_id: str, count: int):
    """Increment the messages-sent counter for a bot in stats.json."""
    if count <= 0:
        return
    stats = load_json(STATS_FILE, {})
    b = stats.get(bot_id, {})
    b["messages_sent"] = b.get("messages_sent", 0) + count
    stats[bot_id] = b
    save_json(STATS_FILE, stats)


def get_messages_flow() -> dict:
    """Load messages.json in node-based flow format.
    Automatically migrates from the old flat {start_text, buttons[]} format."""
    data = load_json(MESSAGES_FILE, {})
    # Detect old flat format (has "start_text" key)
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
        save_json(MESSAGES_FILE, flow)
        return flow
    if not isinstance(data, dict) or not data:
        return {"start": {"text": "Chào mừng bạn! 👋", "buttons": []}}
    if "start" not in data:
        data["start"] = {"text": "Chào mừng bạn! 👋", "buttons": []}
    return data


def _backup_files_to_zip(zf: zipfile.ZipFile):
    """Write all data files into a ZipFile object."""
    for fname in (ALBUMS_FILE, SUBS_FILE, CONFIG_FILE, BOTS_FILE, MESSAGES_FILE, BANNED_FILE, SLOGANS_FILE):
        try:
            with open(fname, "rb") as f:
                zf.writestr(fname, f.read())
        except FileNotFoundError:
            pass
    for path in glob.glob("subs_*.json"):
        try:
            with open(path, "rb") as f:
                zf.writestr(path, f.read())
        except FileNotFoundError:
            pass
    # Include SQLite database
    if os.path.exists(DB_FILE):
        try:
            zf.writestr(DB_FILE, backup_db_to_bytes())
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


# ===== CLOUDINARY HELPERS =====

def upload_to_cloudinary(file_stream) -> str:
    """Upload an image file stream to Cloudinary and return the secure URL."""
    public_id = f"uploads/{uuid.uuid4().hex}"
    result = cloudinary.uploader.upload(
        file_stream,
        public_id=public_id,
        resource_type="image",
        use_filename=False,
        unique_filename=False,
    )
    return result["secure_url"]


def run_daily_backup():
    """Package JSON data files into a zip and upload to Cloudinary backups/ folder.
    Deletes the previous day's backup to keep only the latest."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    backup_public_id = f"{CLOUDINARY_BACKUP_FOLDER}/backup_{today_str}"
    old_public_id = f"{CLOUDINARY_BACKUP_FOLDER}/backup_{yesterday_str}"

    # Delete yesterday's backup (raw resource type)
    try:
        cloudinary.uploader.destroy(old_public_id, resource_type="raw", invalidate=True)
        log.info(f"Deleted old backup: {old_public_id}")
    except Exception as e:
        log.warning(f"Could not delete old backup {old_public_id}: {e}")

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _backup_files_to_zip(zf)
    buf.seek(0)

    # Upload to Cloudinary as raw file
    result = cloudinary.uploader.upload(
        buf,
        public_id=backup_public_id,
        resource_type="raw",
        use_filename=False,
        unique_filename=False,
    )
    log.info(f"Backup uploaded to Cloudinary: {result.get('secure_url')}")
    return result.get("secure_url")


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
    albums = load_json(ALBUMS_FILE, {})
    subs = load_json(SUBS_FILE, [])
    bots = load_json(BOTS_FILE, [])
    messages_cfg = load_json(MESSAGES_FILE, {
        "start_text": "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
        "buttons": [{"text": "🔥 CHẠM LÀ NGHIỆN 🔥", "callback_data": "menu", "url": ""}],
    })
    cfg = load_json(CONFIG_FILE, {"hour": 0, "minute": 0})
    slogans = load_json(SLOGANS_FILE, {"enabled": True, "items": []})
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
    return render_template("index.html", albums=albums, subs=subs,
                           active_album=active_album, total_posts=total_posts(albums),
                           role=session.get("role", ""), username=session.get("username", ""),
                           bots=bots, messages_cfg=messages_cfg, cfg=cfg, slogans=slogans,
                           id_stats=id_stats)


# --- Album management ---

@app.route("/albums/create", methods=["POST"])
@login_required
def create_album():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title:
        return "Missing title", 400
    album_id = f"album_{uuid.uuid4().hex[:8]}"
    albums = load_json(ALBUMS_FILE, {})
    albums[album_id] = {
        "title": title,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "posts": [],
    }
    save_json(ALBUMS_FILE, albums)
    return redirect(f"/?album={album_id}")


@app.route("/albums/<album_id>/delete", methods=["POST"])
@owner_required
def delete_album(album_id):
    albums = load_json(ALBUMS_FILE, {})
    albums.pop(album_id, None)
    save_json(ALBUMS_FILE, albums)
    return redirect("/")


# --- Post management ---

@app.route("/albums/<album_id>/posts", methods=["POST"])
@login_required
def add_post(album_id):
    caption = request.form.get("caption", "").strip()
    albums = load_json(ALBUMS_FILE, {})
    if album_id not in albums:
        return "Album not found", 404
    photos = []
    for img in request.files.getlist("images"):
        if img and img.filename:
            try:
                url = upload_to_cloudinary(img.stream)
                photos.append({"url": url})
            except Exception as e:
                log.error(f"Cloudinary upload failed: {e}")
    post = {
        "id": uuid.uuid4().hex,
        "caption": caption,
        "photos": photos,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if "posts" not in albums[album_id]:
        albums[album_id]["posts"] = []
    albums[album_id]["posts"].append(post)
    save_json(ALBUMS_FILE, albums)
    return redirect(f"/?album={album_id}")


@app.route("/albums/<album_id>/posts/<post_id>/delete", methods=["POST"])
@owner_required
def delete_post(album_id, post_id):
    albums = load_json(ALBUMS_FILE, {})
    if album_id in albums:
        albums[album_id]["posts"] = [
            p for p in albums[album_id].get("posts", []) if p["id"] != post_id
        ]
        save_json(ALBUMS_FILE, albums)
    return redirect(f"/?album={album_id}")


# --- Legacy routes (kept for backward compatibility) ---

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    album_id = request.form.get("album_id", "").strip()
    caption_text = request.form.get("caption", "").strip()
    if not album_id:
        return "Missing album_id", 400
    albums = load_json(ALBUMS_FILE, {})
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
                url = upload_to_cloudinary(img.stream)
                photos.append({"url": url})
            except Exception as e:
                log.error(f"Cloudinary upload failed: {e}")
    if photos:
        post = {
            "id": uuid.uuid4().hex,
            "caption": caption_text,
            "photos": photos,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        albums[album_id].setdefault("posts", []).append(post)
    save_json(ALBUMS_FILE, albums)
    return redirect("/")


@app.route("/delete/<album_id>")
@owner_required
def delete(album_id):
    albums = load_json(ALBUMS_FILE, {})
    albums.pop(album_id, None)
    save_json(ALBUMS_FILE, albums)
    return redirect("/")


@app.route("/healthz")
def healthz():
    return "OK", 200


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
    """Trigger a manual backup to Cloudinary and return JSON result."""
    try:
        url = run_daily_backup()
        return jsonify({"ok": True, "url": url})
    except Exception as e:
        log.error(f"Manual backup failed: {e}")
        return jsonify({"ok": False, "error": "Sao lưu thất bại. Vui lòng thử lại."}), 500


@app.route("/backup/restore", methods=["POST"])
@owner_required
def backup_restore():
    """Fetch the latest backup from Cloudinary and restore JSON data files."""
    backup_url = None
    for days_ago in range(0, 7):
        date_str = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        public_id = f"{CLOUDINARY_BACKUP_FOLDER}/backup_{date_str}"
        try:
            resource = cloudinary.api.resource(public_id, resource_type="raw")
            backup_url = resource.get("secure_url")
            if backup_url:
                break
        except Exception:
            continue

    if not backup_url:
        return jsonify({"ok": False, "error": "Không tìm thấy bản sao lưu trên Cloudinary"}), 404

    # Validate that the URL is a secure Cloudinary URL to prevent SSRF
    if not (backup_url.startswith("https://") and "cloudinary.com" in backup_url):
        log.error(f"Restore aborted: unexpected backup URL: {backup_url}")
        return jsonify({"ok": False, "error": "URL sao lưu không hợp lệ"}), 400

    try:
        max_bytes = 10 * 1024 * 1024  # 10 MB limit
        with urllib.request.urlopen(backup_url, timeout=30) as resp:  # noqa: S310
            zip_data = resp.read(max_bytes)

        buf = io.BytesIO(zip_data)
        allowed_static = {ALBUMS_FILE, SUBS_FILE, CONFIG_FILE, BOTS_FILE, MESSAGES_FILE, BANNED_FILE}
        with zipfile.ZipFile(buf, "r") as zf:
            for fname in zf.namelist():
                if fname == DB_FILE:
                    # Restore SQLite database
                    db_data = zf.read(fname)
                    with open(DB_FILE, "wb") as f:
                        f.write(db_data)
                    init_db()  # ensure schema is up to date
                    continue
                if fname not in allowed_static and not (
                    fname.startswith("subs_") and fname.endswith(".json")
                ):
                    continue
                content = json.loads(zf.read(fname).decode("utf-8"))
                save_json(fname, content)

        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Restore failed: {e}")
        return jsonify({"ok": False, "error": "Khôi phục thất bại. Vui lòng thử lại."}), 500


async def _async_run_ids_broadcast(bots: list, message: str, user_ids: list) -> None:
    """Async broadcast to IDs with all bots running concurrently.

    Every active bot processes the full list of user_ids in parallel.  Per-bot
    proactive rate limit: ~25 msg/sec (asyncio.sleep(0.04)).  When a 429 is
    received, only the affected bot sleeps for the Telegram-supplied
    ``retry_after`` duration; all other bots continue uninterrupted.

    After *all* bots have attempted a given uid, the best reachability status
    (active > blocked > invalid) is written to the DB and the global progress
    counter is incremented.
    """
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

    async def bot_worker(bot: dict) -> None:
        token = (bot.get("token") or "").strip()
        bot_id = bot.get("id", "")
        if not token:
            return
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        retry_until = 0.0  # Event-loop clock time after which this bot's 429 cooldown ends

        async with httpx.AsyncClient(timeout=10) as client:
            for uid in user_ids:
                # Wait out any active 429 cooldown for this bot
                now = asyncio.get_running_loop().time()
                if retry_until > now:
                    await asyncio.sleep(retry_until - now)

                new_status: str = "invalid"
                while True:  # Retry loop – only retries on 429
                    try:
                        resp = await client.post(
                            api_url,
                            json={"chat_id": int(uid), "text": message},
                        )
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
                        elif resp.status_code == 403:
                            new_status = "blocked"
                        else:
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
) -> None:
    """Async broadcast to active IDs with workload distributed across all bots.

    User IDs are split among bots via interleaved slicing so that N bots each
    handle ~1/N of the audience, achieving N × ~25 msg/sec total throughput.
    Per-bot 429 handling pauses only the affected bot; others continue.
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

    async def bot_worker(bot: dict, uid_slice: list) -> None:
        nonlocal total_success, total_fail, done_count
        token = (bot.get("token") or "").strip()
        bot_id = bot.get("id", "")
        if not token:
            return
        retry_until = 0.0  # Event-loop clock time after which this bot's 429 cooldown ends

        async with httpx.AsyncClient(timeout=10) as client:
            for uid in uid_slice:
                now = asyncio.get_running_loop().time()
                if retry_until > now:
                    await asyncio.sleep(retry_until - now)

                endpoint, payload = _build_telegram_payload(uid, message, media_type, media_url, buttons)
                if payload is None:
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
    """Send a broadcast message to all subscribers via all configured bots."""
    bots_list = _get_all_active_bots()
    if not bots_list:
        return jsonify({"ok": False, "error": "Bot chưa được cấu hình"}), 500
    message = request.form.get("message", "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Tin nhắn không được để trống"}), 400
    subs = load_json(SUBS_FILE, [])
    if not subs:
        return jsonify({"ok": False, "error": "Chưa có người đăng ký nào"}), 400
    success, fail = 0, 0
    for bot in bots_list:
        token = (bot.get("token") or "").strip()
        if not token:
            continue
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        for user_id in subs:
            try:
                resp = httpx.post(api_url, json={"chat_id": user_id, "text": message}, timeout=10)
                if resp.status_code == 200:
                    success += 1
                else:
                    fail += 1
            except Exception as e:
                log.warning(f"Broadcast to {user_id} failed: {e}")
                fail += 1
    return jsonify({"ok": True, "success": success, "fail": fail})


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
    import time as _time

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

    if not message and media_type == "none":
        return jsonify({"ok": False, "error": "Vui lòng nhập nội dung tin nhắn hoặc chọn media"}), 400
    if media_type in ("image", "video") and not media_url:
        return jsonify({"ok": False, "error": "Vui lòng nhập URL media"}), 400

    # Parse buttons
    buttons = []
    if buttons_raw:
        try:
            buttons = _json.loads(buttons_raw)
            if not isinstance(buttons, list):
                buttons = []
        except Exception:
            return jsonify({"ok": False, "error": "buttons_json không hợp lệ"}), 400

    # Fetch active user IDs
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM telegram_ids WHERE status = 'active'"
            ).fetchall()
        user_ids = [r["user_id"] for r in rows]
    except Exception as e:
        log.error(f"broadcast_all DB read error: {e}")
        return jsonify({"ok": False, "error": "Lỗi đọc database. Vui lòng thử lại."}), 500

    if not user_ids:
        return jsonify({"ok": False, "error": "Không có ID active nào để gửi. Hãy chạy Broadcast to IDs trước."}), 400

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
        log.warning(f"broadcast_logs insert failed: {e}")

    t = threading.Thread(
        target=lambda: asyncio.run(
            _async_run_broadcast_all(bots_list, user_ids, message, media_type, media_url, buttons, campaign_id)
        ),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "total": len(user_ids), "bots_count": len(bots_list), "message": "Broadcast All đã bắt đầu"})


@app.route("/settings/time", methods=["POST"])
@login_required
def settings_time():
    """Update auto-schedule hour and minute in config.json (Vietnam time, UTC+7)."""
    try:
        hour = int(request.form.get("hour", 0))
        minute = int(request.form.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return jsonify({"ok": False, "error": "Giờ hoặc phút không hợp lệ"}), 400
        cfg = load_json(CONFIG_FILE, {})
        cfg["hour"] = hour
        cfg["minute"] = minute
        save_json(CONFIG_FILE, cfg)
        return jsonify({"ok": True, "hour": hour, "minute": minute})
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Dữ liệu không hợp lệ"}), 400


@app.route("/bots/add", methods=["POST"])
@owner_required
def add_bot():
    """Add a new bot to bots.json."""
    name = request.form.get("name", "").strip()
    token = request.form.get("token", "").strip()
    admin_id_str = request.form.get("admin_id", "").strip()
    if not name or not token:
        return jsonify({"ok": False, "error": "Tên và Token không được để trống"}), 400
    if admin_id_str and not admin_id_str.isdigit():
        return jsonify({"ok": False, "error": "Admin ID phải là số nguyên dương"}), 400
    admin_id_val = int(admin_id_str) if admin_id_str else None
    bots = load_json(BOTS_FILE, [])
    bots.append({
        "id": uuid.uuid4().hex,
        "name": name,
        "token": token,
        "admin_id": admin_id_val,
    })
    save_json(BOTS_FILE, bots)
    return jsonify({"ok": True})


@app.route("/bots/<bot_id>/delete", methods=["POST"])
@owner_required
def delete_bot(bot_id):
    """Remove a bot from bots.json."""
    bots = load_json(BOTS_FILE, [])
    bots = [b for b in bots if b.get("id") != bot_id]
    save_json(BOTS_FILE, bots)
    return jsonify({"ok": True})


@app.route("/bots/health", methods=["GET"])
@owner_required
def bots_health():
    """Return health status and per-bot stats for all bots in bots.json."""
    bots_list = load_json(BOTS_FILE, [])
    stats = load_json(STATS_FILE, {})
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
        bot_subs = load_json(subs_file(bot_id), [])
        if not bot_subs:
            bot_subs = load_json(SUBS_FILE, [])
        bot_stats = stats.get(bot_id, {})
        result.append({
            "id": bot_id,
            "status": status,
            "subs_count": len(bot_subs),
            "messages_sent": bot_stats.get("messages_sent", 0),
        })
    return jsonify(result)


@app.route("/bots/<bot_id>/token", methods=["GET"])
@owner_required
def get_bot_token(bot_id):
    """Return the full token of a bot (owner only)."""
    bots = load_json(BOTS_FILE, [])
    bot = next((b for b in bots if b.get("id") == bot_id), None)
    if not bot:
        return jsonify({"ok": False, "error": "Bot không tồn tại"}), 404
    return jsonify({"ok": True, "token": bot.get("token", "")})


def _get_all_active_bots() -> list:
    """Return list of all configured bots with valid tokens."""
    bots_list = load_json(BOTS_FILE, [])
    if not bots_list and BOT_TOKEN:
        bots_list = [{"id": "env_default", "name": "Default Bot", "token": BOT_TOKEN}]
    return [b for b in bots_list if (b.get("token") or "").strip()]


def _build_telegram_payload(uid: str, message: str, media_type: str, media_url: str, buttons: list) -> tuple:
    """Build (endpoint_suffix, payload_dict) for a Telegram API call with optional media and buttons."""
    import json as _json
    reply_markup = None
    if buttons:
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
        if inline_keyboard:
            reply_markup = _json.dumps({"inline_keyboard": inline_keyboard})

    chat_id_str = str(uid)
    chat_id = int(chat_id_str) if (chat_id_str.startswith("-") and chat_id_str[1:].isdigit()) or chat_id_str.isdigit() else uid
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
    save_json(MESSAGES_FILE, clean_flow)
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
        if label and value and btn_type in ("url", "node"):
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
    save_json(MESSAGES_FILE, flow)
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
    save_json(MESSAGES_FILE, flow)
    return jsonify({"ok": True})


@app.route("/slogans", methods=["GET"])
@login_required
def get_slogans():
    """Return current slogans configuration."""
    data = load_json(SLOGANS_FILE, {"enabled": True, "items": []})
    return jsonify(data)


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
    save_json(SLOGANS_FILE, {"enabled": enabled, "items": clean_items})
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
    """Broadcast a message to all uploaded IDs using ALL configured bots, classifying status via Telegram API errors."""
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
    if not message:
        return jsonify({"ok": False, "error": "Tin nhắn không được để trống"}), 400

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id FROM telegram_ids WHERE status IN ('unknown', 'active')"
            ).fetchall()
        user_ids = [r["user_id"] for r in rows]
    except Exception as e:
        log.error(f"ids_broadcast DB read error: {e}")
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
        target=lambda: asyncio.run(_async_run_ids_broadcast(bots_list, message, user_ids)),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "total": len(user_ids), "bots_count": len(bots_list), "message": "Broadcast đã bắt đầu"})


# ===== BOT HANDLERS =====

if BOT_TOKEN or load_json(BOTS_FILE, []):
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

    # ---- Flow keyboard builder ----

    def _build_flow_keyboard(buttons: list) -> list:
        """Build InlineKeyboardMarkup rows from a node's button list."""
        keyboard = []
        for btn in buttons:
            label = btn.get("label", "")
            btn_type = btn.get("type", "url")
            value = btn.get("value", "")
            if not label or not value:
                continue
            if btn_type == "url":
                keyboard.append([InlineKeyboardButton(label, url=value)])
            else:  # node
                keyboard.append([InlineKeyboardButton(label, callback_data=f"flow_{value}")])
        return keyboard

    # ---- Shared handlers (no per-bot subscriber state needed) ----

    async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        albums = load_json(ALBUMS_FILE, {})
        buttons = []
        for key, val in sorted(albums.items()):
            title = val.get("title", key) if isinstance(val, dict) else key
            buttons.append([InlineKeyboardButton(f"🔥 {title}", callback_data=key)])
        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])

        await query.edit_message_text("Chọn album:", reply_markup=InlineKeyboardMarkup(buttons))

    async def send_album(chat_id, bot, album):
        """Send all posts in an album (supports both new posts[] format and legacy photos[] format)."""
        if "posts" in album:
            groups = [(post.get("photos", []), post.get("caption", "")) for post in album["posts"]]
        else:
            groups = [(album.get("photos", []), "")]

        for photos_list, group_caption in groups:
            media = []
            open_files = []
            try:
                for i, p in enumerate(photos_list):
                    url = p["url"]
                    if url.startswith("/static/uploads/"):
                        f = open(url.lstrip("/"), "rb")
                        open_files.append(f)
                        media_src = f
                    else:
                        media_src = url
                    caption = group_caption if i == 0 else ""
                    media.append(InputMediaPhoto(media_src, caption=caption))
                if media:
                    await bot.send_media_group(chat_id, media)
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

        albums = load_json(ALBUMS_FILE, {})
        album = albums.get(query.data)
        chat_id = query.message.chat.id
        await query.delete_message()

        if album:
            await send_album(chat_id, context.bot, album)

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

        _last_sent = [None]  # mutable list so the nested coroutine can update it
        _subs_file = subs_file(bot_cfg_id)

        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_chat.id
            banned = load_json(BANNED_FILE, [])
            if user_id in banned:
                await update.message.reply_text("⛔ Bạn đã bị cấm sử dụng bot này.")
                return
            bot_subs = load_json(_subs_file, [])
            if user_id not in bot_subs:
                bot_subs.append(user_id)
                save_json(_subs_file, bot_subs)

            # Send slogans sequentially if enabled
            slogans_cfg = load_json(SLOGANS_FILE, {"enabled": False, "items": []})
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

        async def set_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            try:
                time_str = update.message.text.split("_")[1]
                h, m = map(int, time_str.split(":"))
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    await update.message.reply_text("Giờ/phút không hợp lệ (0-23 / 0-59)")
                    return
                save_json(CONFIG_FILE, {"hour": h, "minute": m})
                await update.message.reply_text(f"✅ Đã set giờ: {h:02d}:{m:02d} (Giờ VN)")
            except Exception:
                await update.message.reply_text("Sai format. Dùng: /settudongguilink_HH:MM")

        async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            bot_subs = load_json(_subs_file, [])
            albums = load_json(ALBUMS_FILE, {})
            total = total_posts(albums)
            stats = load_json(STATS_FILE, {})
            sent = stats.get(bot_cfg_id, {}).get("messages_sent", 0)
            await update.message.reply_text(
                f"📊 Thống kê Bot:\n"
                f"👥 Người đăng ký: {len(bot_subs)}\n"
                f"📁 Albums: {len(albums)}\n"
                f"📝 Bài viết: {total}\n"
                f"📤 Tin đã gửi: {sent}"
            )

        async def sendall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not bot_admin_id or update.effective_chat.id != bot_admin_id:
                return
            message_text = " ".join(context.args) if context.args else ""
            if not message_text:
                await update.message.reply_text("Cú pháp: /sendall <nội dung tin nhắn>")
                return
            bot_subs = load_json(_subs_file, [])
            if not bot_subs:
                await update.message.reply_text("Chưa có người đăng ký nào.")
                return
            success, fail = 0, 0
            for user_id in bot_subs:
                try:
                    await context.bot.send_message(user_id, message_text)
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
            bot_subs = load_json(_subs_file, [])
            if target_id in bot_subs:
                bot_subs.remove(target_id)
                save_json(_subs_file, bot_subs)
            banned = load_json(BANNED_FILE, [])
            if target_id not in banned:
                banned.append(target_id)
                save_json(BANNED_FILE, banned)
            await update.message.reply_text(f"✅ Đã cấm người dùng {target_id}.")

        async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
            now_vn = datetime.now(timezone.utc) + timedelta(hours=7)
            cfg = load_json(CONFIG_FILE, {"hour": 0, "minute": 0})
            if now_vn.hour == cfg["hour"] and now_vn.minute == cfg["minute"]:
                key = f"{now_vn.hour}:{now_vn.minute}"
                if _last_sent[0] != key:
                    _last_sent[0] = key
                    albums = load_json(ALBUMS_FILE, {})
                    if not albums:
                        return
                    latest = sorted(albums.keys())[-1]
                    bot_subs = load_json(_subs_file, [])
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
        ptb_app.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))
        ptb_app.add_handler(CallbackQueryHandler(flow_handler, pattern="^flow_"))
        ptb_app.add_handler(CallbackQueryHandler(album_click))
        ptb_app.job_queue.run_repeating(scheduler_job, interval=30, first=1)
        ptb_app.job_queue.run_daily(daily_backup_job, time=dt_time(1, 0, 0, tzinfo=timezone.utc))
        return ptb_app

    def run_bot_thread():
        """Start all configured bots concurrently in one asyncio event loop."""
        bots_cfg = load_json(BOTS_FILE, [])
        # Fallback: if bots.json is empty, use the env-var token to bootstrap
        if not bots_cfg:
            if BOT_TOKEN:
                bots_cfg = [{"id": "env_default", "name": "Default Bot",
                              "token": BOT_TOKEN, "admin_id": ADMIN_ID}]
            else:
                log.warning("No bots configured – bot disabled")
                return

        async def _run_all():
            ptb_apps = []
            for cfg_entry in bots_cfg:
                token = (cfg_entry.get("token") or "").strip()
                if not token:
                    log.warning(f"Bot '{cfg_entry.get('name', '?')}' has no token – skipping")
                    continue
                _aid_raw = cfg_entry.get("admin_id")
                _aid = int(_aid_raw) if _aid_raw and str(_aid_raw).isdigit() else ADMIN_ID
                _bid = cfg_entry.get("id", "env_default")
                ptb_apps.append(_build_ptb_app(token, _aid, _bid))

            if not ptb_apps:
                log.warning("No valid bot tokens found – bot disabled")
                return

            for a in ptb_apps:
                await a.initialize()
                await a.start()
                await a.updater.start_polling(
                    allowed_updates=["message", "callback_query"],
                    drop_pending_updates=True,
                )
            log.info(f"✅ {len(ptb_apps)} bot(s) started successfully")

            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                for a in reversed(ptb_apps):
                    try:
                        await a.updater.stop()
                        await a.stop()
                        await a.shutdown()
                    except Exception as exc:
                        log.warning(f"Error stopping bot: {exc}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_all())
        except Exception as e:
            log.error(f"Bot thread error: {e}")
            raise

else:
    log.warning("No bot token configured – bot disabled, Flask only")


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

    # Only start the scheduler once (guard against werkzeug reloader double-start)
    if not os.environ.get("WERKZEUG_RUN_MAIN") == "false":
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
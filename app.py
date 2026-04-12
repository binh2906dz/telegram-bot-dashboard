import io
import os
import json
import uuid
import asyncio
import logging
import zipfile
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, render_template, jsonify, Response

import cloudinary
import cloudinary.uploader
import cloudinary.api

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_admin_str = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_ID = int(_admin_str) if _admin_str.isdigit() else None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

ALBUMS_FILE = "albums.json"
SUBS_FILE = "subscribers.json"
CONFIG_FILE = "config.json"

# ===== CLOUDINARY CONFIG =====
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "dklzj37zf"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "769787428559636"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "1AA05R--Hd9qClIRiN1_wB8LaI4"),
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


def total_posts(albums):
    return sum(len(a.get("posts", [])) for a in albums.values() if isinstance(a, dict))


# ===== CLOUDINARY HELPERS =====

def upload_to_cloudinary(file_stream, original_filename: str) -> str:
    """Upload a file stream to Cloudinary and return the secure URL."""
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
        for fname in (ALBUMS_FILE, SUBS_FILE, CONFIG_FILE):
            try:
                with open(fname, "rb") as f:
                    zf.writestr(fname, f.read())
            except FileNotFoundError:
                zf.writestr(fname, b"{}")
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

@app.route("/")
def index():
    albums = load_json(ALBUMS_FILE, {})
    subs = load_json(SUBS_FILE, [])
    active_album = request.args.get("album", "")
    return render_template("index.html", albums=albums, subs=subs,
                           active_album=active_album, total_posts=total_posts(albums))


# --- Album management ---

@app.route("/albums/create", methods=["POST"])
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
def delete_album(album_id):
    albums = load_json(ALBUMS_FILE, {})
    albums.pop(album_id, None)
    save_json(ALBUMS_FILE, albums)
    return redirect("/")


# --- Post management ---

@app.route("/albums/<album_id>/posts", methods=["POST"])
def add_post(album_id):
    caption = request.form.get("caption", "").strip()
    albums = load_json(ALBUMS_FILE, {})
    if album_id not in albums:
        return "Album not found", 404
    photos = []
    for img in request.files.getlist("images"):
        if img and img.filename:
            try:
                url = upload_to_cloudinary(img.stream, img.filename)
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
                url = upload_to_cloudinary(img.stream, img.filename)
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
def delete(album_id):
    albums = load_json(ALBUMS_FILE, {})
    albums.pop(album_id, None)
    save_json(ALBUMS_FILE, albums)
    return redirect("/")


@app.route("/healthz")
def healthz():
    return "OK", 200


@app.route("/backup/download")
def backup_download():
    """Admin-only endpoint to download a manual backup of all JSON data files."""
    admin_key = os.environ.get("ADMIN_SECRET_KEY", "")
    provided_key = request.args.get("key", "")
    if admin_key and provided_key != admin_key:
        return "Unauthorized", 401

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in (ALBUMS_FILE, SUBS_FILE, CONFIG_FILE):
            try:
                with open(fname, "rb") as f:
                    zf.writestr(fname, f.read())
            except FileNotFoundError:
                zf.writestr(fname, b"{}")
    buf.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Response(
        buf.read(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=backup_{timestamp}.zip"},
    )


# ===== BOT HANDLERS =====

if BOT_TOKEN:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

    _last_sent = None

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_chat.id
        subs = load_json(SUBS_FILE, [])
        if user_id not in subs:
            subs.append(user_id)
            save_json(SUBS_FILE, subs)

        m1 = await update.message.reply_text("🚀 NƠI BÓNG TỐI BẮT ĐẦU... NƠI BẢN NĂNG THỨC TỈNH 🚀")
        await asyncio.sleep(1)
        m2 = await update.message.reply_text("👿 KHÔNG DÀNH CHO NGƯỜI YẾU TIM - CHỈ DÀNH CHO KẺ DÁM KHÁM PHÁ 👿")
        await asyncio.sleep(1)
        m3 = await update.message.reply_text("👀 BƯỚC VÀO ĐÂY BẠN SẼ KHÔNG MUỐN QUAY LẠI 👀")
        await asyncio.sleep(2)
        for m in [m1, m2, m3]:
            try:
                await m.delete()
            except Exception:
                pass

        keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
        await update.message.reply_text(
            "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

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
        # Normalise to a list of (photos_list, caption) tuples
        if "posts" in album:
            groups = [(post.get("photos", []), post.get("caption", "")) for post in album["posts"]]
        else:
            # Legacy format: album["photos"] – treat as a single group
            groups = [(album.get("photos", []), "")]

        for photos_list, group_caption in groups:
            media = []
            open_files = []
            try:
                for i, p in enumerate(photos_list):
                    url = p["url"]
                    if url.startswith("/static/uploads/"):
                        # Legacy local file fallback
                        f = open(url.lstrip("/"), "rb")
                        open_files.append(f)
                        media_src = f
                    else:
                        # Cloudinary or any external URL – use directly
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
            keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
            await query.edit_message_text(
                "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        albums = load_json(ALBUMS_FILE, {})
        album = albums.get(query.data)
        chat_id = query.message.chat.id
        await query.delete_message()

        if album:
            await send_album(chat_id, context.bot, album)

    async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ADMIN_ID or update.effective_chat.id != ADMIN_ID:
            return
        try:
            time_str = update.message.text.split("_")[1]
            h, m = map(int, time_str.split(":"))
            h_utc = (h - 7) % 24
            save_json(CONFIG_FILE, {"hour": h_utc, "minute": m})
            await update.message.reply_text(f"Đã set giờ: {h}:{m} (VN)")
        except Exception:
            await update.message.reply_text("Sai format")

    async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
        global _last_sent
        now = datetime.now(timezone.utc)
        cfg = load_json(CONFIG_FILE, {"hour": 0, "minute": 0})

        if now.hour == cfg["hour"] and now.minute == cfg["minute"]:
            key = f"{now.hour}:{now.minute}"
            if _last_sent != key:
                _last_sent = key
                albums = load_json(ALBUMS_FILE, {})
                if not albums:
                    return
                latest = sorted(albums.keys())[-1]
                subs = load_json(SUBS_FILE, [])
                success, fail = 0, 0
                for u in subs:
                    try:
                        await send_album(u, context.bot, albums[latest])
                        success += 1
                    except Exception:
                        fail += 1
                if ADMIN_ID:
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"📊 Report\nUsers: {len(subs)}\nOK: {success}\nFail: {fail}",
                    )

    async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
        """APScheduler job that runs once a day to back up JSON data to Cloudinary."""
        try:
            url = run_daily_backup()
            if ADMIN_ID:
                await context.bot.send_message(ADMIN_ID, f"✅ Backup completed!\n🔗 {url}")
        except Exception as e:
            log.error(f"Daily backup failed: {e}")
            if ADMIN_ID:
                await context.bot.send_message(ADMIN_ID, f"❌ Backup failed: {e}")

    def run_bot_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        ptb_app = Application.builder().token(BOT_TOKEN).build()
        ptb_app.add_handler(CommandHandler("start", start))
        ptb_app.add_handler(CommandHandler("settudongguilink", set_time))
        ptb_app.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))
        ptb_app.add_handler(CallbackQueryHandler(album_click))
        ptb_app.job_queue.run_repeating(scheduler_job, interval=30, first=1)
        # Daily backup at 01:00 UTC
        from datetime import time as dt_time
        ptb_app.job_queue.run_daily(daily_backup_job, time=dt_time(1, 0, 0, tzinfo=timezone.utc))

        log.info("Bot starting with run_polling in thread...")
        try:
            ptb_app.run_polling(
                allowed_updates=["message", "callback_query"],
                stop_signals=None,
                drop_pending_updates=True,
            )
        except Exception as e:
            log.error(f"Bot thread error: {e}")
            raise

else:
    log.warning("TELEGRAM_BOT_TOKEN not set – bot disabled, Flask only")

# ===== MAIN =====
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
import os
import json
import uuid
import asyncio
import logging
import threading
import datetime
from functools import wraps

from flask import Flask, request, jsonify, render_template, redirect, url_for
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("TOKEN", "")
_admin_raw = os.environ.get("ADMIN_ID", "0")
try:
    ADMIN_ID = int(_admin_raw)
except (ValueError, TypeError):
    ADMIN_ID = 0
    log.warning("ADMIN_ID env var is missing or not an integer; admin features disabled.")

VN_OFFSET = 7

# ===== FLASK APP =====
flask_app = Flask(__name__)

# ===== JSON HELPERS =====
def load_json(file, default):
    try:
        with open(file) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ===== BOT HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id

    subs = load_json("subscribers.json", [])
    if user_id not in subs:
        subs.append(user_id)
        save_json("subscribers.json", subs)
        log.info("New subscriber: %s", user_id)

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


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    log.debug("Callback received: %s", data)

    if data in ("menu", "back_to_main"):
        albums = load_json("albums.json", {})
        buttons = []

        for key in sorted(albums.keys()):
            date = key.replace("album_", "").replace("_", "-")
            label = f"Link🔥 {date[8:10]}-{date[5:7]}" if len(date) >= 10 else f"Link🔥 {key}"
            buttons.append([InlineKeyboardButton(label, callback_data=key)])

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back_to_main")])
        await query.edit_message_text(
            "Chọn album:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Album click — data is an album key like "album_2026_04_11"
    # Save chat.id BEFORE deleting the message (PTB v20: use chat.id not chat_id)
    cid = query.message.chat.id
    albums = load_json("albums.json", {})
    album = albums.get(data)

    try:
        await query.delete_message()
    except Exception as e:
        log.warning("Could not delete message: %s", e)

    if album:
        await send_album(cid, context.bot, album)
    else:
        await context.bot.send_message(
            cid,
            "📭 Chưa có album.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Quay lại", callback_data="back_to_main")]
            ]),
        )


async def send_album(chat_id, bot, album):
    """Send all photos in an album as a media group."""
    photos = album.get("photos", [])
    if not photos:
        log.warning("Album has no photos for chat %s", chat_id)
        return

    media = []
    for i, p in enumerate(photos):
        if i == 0:
            media.append(InputMediaPhoto(p["url"], caption=p.get("caption", "")))
        else:
            media.append(InputMediaPhoto(p["url"]))

    await bot.send_media_group(chat_id, media)


async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return

    try:
        # Format: /settudongguilink_HH:MM  (underscore separator)
        time_str = update.message.text.split("_", 1)[1]
        h, m = map(int, time_str.split(":"))
        h_utc = (h - VN_OFFSET) % 24
        save_json("config.json", {"hour": h_utc, "minute": m})
        await update.message.reply_text(
            f"✅ Đã set giờ: {h:02d}:{m:02d} (VN) → {h_utc:02d}:{m:02d} (UTC)"
        )
        log.info("Broadcast time set to VN %02d:%02d (UTC %02d:%02d)", h, m, h_utc, m)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Sai format. Dùng: /settudongguilink_HH:MM\nLỗi: {e}"
        )


# ===== SCHEDULER (runs inside bot event loop via job_queue) =====
_last_sent_key: str | None = None


async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    """Called every 30 s by PTB job_queue; broadcasts latest album at scheduled time."""
    global _last_sent_key
    try:
        now = datetime.datetime.utcnow()
        cfg = load_json("config.json", {"hour": 0, "minute": 0})

        if now.hour == cfg.get("hour", 0) and now.minute == cfg.get("minute", 0):
            key = f"{now.hour}:{now.minute}"
            if _last_sent_key == key:
                return  # already sent this minute
            _last_sent_key = key

            albums = load_json("albums.json", {})
            if not albums:
                log.info("Scheduler: no albums to send.")
                return

            latest = sorted(albums.keys())[-1]
            subs = load_json("subscribers.json", [])
            success, fail = 0, 0

            for uid in subs:
                try:
                    await send_album(uid, context.bot, albums[latest])
                    success += 1
                except Exception as e:
                    log.warning("Broadcast failed for %s: %s", uid, e)
                    fail += 1

            log.info("Broadcast done. OK=%d Fail=%d", success, fail)

            if ADMIN_ID:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"📊 Broadcast Report\nUsers: {len(subs)}\nOK: {success}\nFail: {fail}",
                )
    except Exception as e:
        log.error("Scheduler error: %s", e)


# ===== BOT THREAD =====
def run_bot_thread():
    if not BOT_TOKEN:
        log.error("TOKEN env var is not set. Telegram bot will not start.")
        return

    log.info("Starting bot thread…")
    try:
        bot_app = Application.builder().token(BOT_TOKEN).build()

        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(CommandHandler("settudongguilink", set_time))
        bot_app.add_handler(CallbackQueryHandler(handle_callback))

        # Schedule broadcast checker every 30 s
        bot_app.job_queue.run_repeating(scheduler_job, interval=30, first=5)

        log.info("Bot polling starting (PTB v20)…")
        # run_polling() manages its own event loop (synchronous call)
        bot_app.run_polling(allowed_updates=["message", "callback_query"])
    except Exception as e:
        log.error("Bot thread crashed: %s", e, exc_info=True)


# ===== FLASK ROUTES =====

@flask_app.route("/")
def index():
    albums = load_json("albums.json", {})
    subs = load_json("subscribers.json", [])
    return render_template("index.html", albums=albums, subs=subs)


@flask_app.route("/upload", methods=["POST"])
def upload():
    album_id = request.form.get("album_id", "").strip()
    images = request.files.getlist("images")

    if not album_id or not images:
        return "Missing album_id or images", 400

    albums = load_json("albums.json", {})
    photos = []
    for img in images:
        if img and img.filename:
            fname = f"{uuid.uuid4().hex}_{img.filename}"
            save_dir = os.path.join("static", "uploads")
            os.makedirs(save_dir, exist_ok=True)
            path = os.path.join(save_dir, fname)
            img.save(path)
            photos.append({"url": f"/static/uploads/{fname}", "caption": ""})

    if photos:
        albums[album_id] = {"photos": photos}
        save_json("albums.json", albums)
        log.info("Album uploaded: %s (%d photos)", album_id, len(photos))

    return redirect(url_for("index"))


@flask_app.route("/delete/<album_id>")
def delete_album(album_id):
    albums = load_json("albums.json", {})
    if album_id in albums:
        del albums[album_id]
        save_json("albums.json", albums)
        log.info("Album deleted: %s", album_id)
    return redirect(url_for("index"))


# ===== MAIN =====
if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot_thread, daemon=True, name="telegram-bot")
    bot_thread.start()

    port = int(os.environ.get("PORT", 5000))
    log.info("Flask starting on port %d", port)
    flask_app.run(host="0.0.0.0", port=port)

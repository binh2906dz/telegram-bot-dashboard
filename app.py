import os
import json
import uuid
import asyncio
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, request, redirect, render_template, jsonify

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "") or os.environ.get("TOKEN", "")
ADMIN_ID = os.environ.get("ADMIN_CHAT_ID", "") or os.environ.get("ADMIN_ID", "")
PORT = int(os.environ.get("PORT", 5000))

VN_UTC_OFFSET = 7  # Vietnam is UTC+7

ALBUMS_FILE = "albums.json"
SUBS_FILE = "subscribers.json"
CONFIG_FILE = "config.json"

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ===== DATA HELPERS =====

def load_json(file, default):
    try:
        with open(file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(file, data):
    os.makedirs(os.path.dirname(os.path.abspath(file)), exist_ok=True)
    with open(file, "w") as f:
        json.dump(data, f, indent=2)


# ===== FLASK APP =====

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    albums = load_json(ALBUMS_FILE, {})
    subs = load_json(SUBS_FILE, [])
    return render_template("index.html", albums=albums, subs=subs)


@app.route("/upload", methods=["POST"])
def upload():
    album_id = request.form.get("album_id", "").strip()
    if not album_id:
        return redirect("/")

    images = request.files.getlist("images")
    photos = []
    base_url = request.host_url.rstrip("/")

    for img in images:
        if img and img.filename:
            ext = os.path.splitext(img.filename)[1] or ".jpg"
            filename = uuid.uuid4().hex + ext
            img.save(os.path.join(UPLOAD_DIR, filename))
            photos.append({"url": f"{base_url}/static/uploads/{filename}", "caption": ""})

    if photos:
        albums = load_json(ALBUMS_FILE, {})
        albums[album_id] = {"photos": photos}
        save_json(ALBUMS_FILE, albums)

    return redirect("/")


@app.route("/delete/<album_id>")
def delete_album(album_id):
    albums = load_json(ALBUMS_FILE, {})
    albums.pop(album_id, None)
    save_json(ALBUMS_FILE, albums)
    return redirect("/")


# ===== TELEGRAM BOT =====

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
    PTB_AVAILABLE = True
except ImportError:
    PTB_AVAILABLE = False
    log.warning("python-telegram-bot not installed, bot disabled")


if PTB_AVAILABLE:

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_chat.id
        subs = load_json(SUBS_FILE, [])
        if cid not in subs:
            subs.append(cid)
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

    async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        albums = load_json(ALBUMS_FILE, {})
        buttons = []

        for key in sorted(albums.keys()):
            date = key.replace("album_", "").replace("_", "-")
            label = f"Link🔥 {date[8:10]}-{date[5:7]}" if len(date) >= 10 else f"Link🔥 {key}"
            buttons.append([InlineKeyboardButton(label, callback_data=key)])

        if not buttons:
            cid = q.message.chat.id
            await context.bot.send_message(
                cid,
                "📭 Chưa có album.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_to_main")]]
                ),
            )
            return

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back_to_main")])
        await q.edit_message_text("Chọn album:", reply_markup=InlineKeyboardMarkup(buttons))

    async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
        await q.edit_message_text(
            "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def album_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        albums = load_json(ALBUMS_FILE, {})
        album = albums.get(q.data)
        cid = q.message.chat.id

        await q.delete_message()

        if album:
            media = []
            for i, p in enumerate(album.get("photos", [])):
                if i == 0:
                    media.append(InputMediaPhoto(p["url"], caption=p.get("caption", "")))
                else:
                    media.append(InputMediaPhoto(p["url"]))
            if media:
                await context.bot.send_media_group(cid, media)

    async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            admin_id = int(ADMIN_ID)
        except (ValueError, TypeError):
            return
        if update.effective_chat.id != admin_id:
            return
        try:
            time_str = update.message.text.split("_")[1]
            h, m = map(int, time_str.split(":"))
            h_utc = (h - VN_UTC_OFFSET) % 24
            save_json(CONFIG_FILE, {"hour": h_utc, "minute": m})
            await update.message.reply_text(f"Đã set giờ: {h}:{m:02d} (VN)")
        except Exception:
            await update.message.reply_text("Sai format. Dùng: /settudongguilink_HH:MM")

    _last_sent = None

    async def _scheduler(bot_app):
        global _last_sent
        while True:
            now = datetime.now(timezone.utc)
            cfg = load_json(CONFIG_FILE, {"hour": 0, "minute": 0})

            if now.hour == cfg.get("hour", 0) and now.minute == cfg.get("minute", 0):
                key = f"{now.hour}:{now.minute}"
                if _last_sent != key:
                    _last_sent = key
                    albums = load_json(ALBUMS_FILE, {})
                    if albums:
                        latest = sorted(albums.keys())[-1]
                        subs = load_json(SUBS_FILE, [])
                        success, fail = 0, 0
                        for uid in subs:
                            try:
                                album = albums[latest]
                                media = []
                                for i, p in enumerate(album.get("photos", [])):
                                    if i == 0:
                                        media.append(InputMediaPhoto(p["url"], caption=p.get("caption", "")))
                                    else:
                                        media.append(InputMediaPhoto(p["url"]))
                                if media:
                                    await bot_app.bot.send_media_group(uid, media)
                                success += 1
                            except Exception:
                                fail += 1

                        try:
                            admin_id = int(ADMIN_ID)
                            await bot_app.bot.send_message(
                                admin_id,
                                f"📊 Report\nUsers: {len(subs)}\nOK: {success}\nFail: {fail}",
                            )
                        except Exception:
                            pass

            await asyncio.sleep(30)

    def _run_bot():
        if not BOT_TOKEN:
            log.warning("TELEGRAM_BOT_TOKEN not set – bot disabled")
            return

        async def _main():
            bot_app = Application.builder().token(BOT_TOKEN).build()
            bot_app.add_handler(CommandHandler("start", start))
            bot_app.add_handler(CommandHandler("settudongguilink", set_time))
            bot_app.add_handler(CallbackQueryHandler(handle_menu, pattern="^menu$"))
            bot_app.add_handler(CallbackQueryHandler(handle_back, pattern="^back_to_main$"))
            bot_app.add_handler(CallbackQueryHandler(album_click))

            await bot_app.initialize()
            await bot_app.start()
            # Use updater.start_polling() so we can run in a background thread
            # without installing signal handlers (run_polling() only works in main thread)
            await bot_app.updater.start_polling(
                allowed_updates=["message", "callback_query"]
            )
            log.info("Telegram bot started")
            await _scheduler(bot_app)  # keeps the loop alive

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main())
        except Exception as exc:
            log.error("Bot thread crashed: %s", exc)
        finally:
            loop.close()


# ===== ENTRYPOINT =====

if __name__ == "__main__":
    if PTB_AVAILABLE:
        threading.Thread(target=_run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

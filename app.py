import os
import json
import uuid
import asyncio
import logging
import threading
from datetime import datetime
from flask import Flask, request, redirect, render_template

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_admin_str = os.environ.get("ADMIN_CHAT_ID", "")
ADMIN_ID = int(_admin_str) if _admin_str.isdigit() else None

app = Flask(__name__)

ALBUMS_FILE = "albums.json"
SUBS_FILE = "subscribers.json"
CONFIG_FILE = "config.json"

def load_json(file, default):
    try:
        with open(file) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

# ===== FLASK ROUTES =====

@app.route("/")
def index():
    albums = load_json(ALBUMS_FILE, {})
    subs = load_json(SUBS_FILE, [])
    return render_template("index.html", albums=albums, subs=subs)

@app.route("/upload", methods=["POST"])
def upload():
    album_id = request.form.get("album_id", "").strip()
    caption_text = request.form.get("caption", "").strip()
    if not album_id:
        return "Missing album_id", 400

    os.makedirs("static/uploads", exist_ok=True)
    albums = load_json(ALBUMS_FILE, {})
    photos = []
    for i, img in enumerate(request.files.getlist("images")):
        if img and img.filename:
            filename = f"{uuid.uuid4()}_{img.filename}"
            save_path = os.path.join("static/uploads", filename)
            img.save(save_path)
            photos.append({"url": f"/static/uploads/{filename}", "caption": caption_text if i == 0 else ""})

    albums[album_id] = {"photos": photos}
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
        for key in sorted(albums.keys()):
            date = key.replace("album_", "").replace("_", "-")
            buttons.append([InlineKeyboardButton(f"Link🔥 {date[8:10]}-{date[5:7]}", callback_data=key)])
        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])

        await query.edit_message_text("Chọn album:", reply_markup=InlineKeyboardMarkup(buttons))

    async def send_album(chat_id, bot, album):
        media = []
        for i, p in enumerate(album["photos"]):
            if i == 0:
                media.append(InputMediaPhoto(p["url"], caption=p.get("caption", "")))
            else:
                media.append(InputMediaPhoto(p["url"]))
        await bot.send_media_group(chat_id, media)

    async def album_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        albums = load_json(ALBUMS_FILE, {})
        album = albums.get(query.data)
        await query.delete_message()

        if album:
            await send_album(query.message.chat.id, context.bot, album)

    async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ADMIN_ID or update.effective_chat.id != ADMIN_ID:
            return
        try:
            time_str = update.message.text.split("_")[1]
            h, m = map(int, time_str.split(':'))
            h_utc = (h - 7) % 24
            save_json(CONFIG_FILE, {"hour": h_utc, "minute": m})
            await update.message.reply_text(f"Đã set giờ: {h}:{m} (VN)")
        except Exception:
            await update.message.reply_text("Sai format")

    async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
        global _last_sent
        now = datetime.utcnow()
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

    def run_bot_thread():
        # Create new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        ptb_app = Application.builder().token(BOT_TOKEN).build()
        ptb_app.add_handler(CommandHandler("start", start))
        ptb_app.add_handler(CommandHandler("settudongguilink", set_time))
        ptb_app.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))
        ptb_app.add_handler(CallbackQueryHandler(album_click))
        ptb_app.job_queue.run_repeating(scheduler_job, interval=30, first=1)

        log.info("Bot starting with run_polling in thread...")
        try:
            ptb_app.run_polling(
                allowed_updates=["message", "callback_query"],
                stop_signals=None,
                drop_pending_updates=True
            )
        except Exception as e:
            log.error(f"Bot thread error: {e}")

    threading.Thread(target=run_bot_thread, daemon=True).start()
    log.info("Bot thread created")
else:
    log.warning("TELEGRAM_BOT_TOKEN not set – bot disabled, Flask only")

# ===== MAIN =====
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
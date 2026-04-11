import os
import json
import asyncio
import logging
import re
import threading
from datetime import datetime, timezone
from flask import Flask

# ===== CONFIG (ĐÃ GẮN SẴN) =====
BOT_TOKEN = "8448093514:AAGK58_UCuF2YK34YghvURfvX0YD5BZ0t1g"
ADMIN_ID = 5914285286
VN_OFFSET = 7

# ===== BASIC =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ===== DATA FILE =====
def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

# ===== FILES =====
SUBS_FILE = "subscribers.json"
ALBUMS_FILE = "albums.json"
CONFIG_FILE = "config.json"

for f, d in [
    (SUBS_FILE, []),
    (ALBUMS_FILE, {}),
    (CONFIG_FILE, {"hour":14,"minute":0})
]:
    if not os.path.exists(f):
        save_json(f, d)

# ===== FLASK =====
app = Flask(__name__)

@app.route("/")
def home():
    return "🚀 TelegramCMS đang chạy"

# ===== TELEGRAM BOT =====
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

def build_app():

    async def start(update, context):
        cid = update.effective_chat.id

        subs = load_json(SUBS_FILE, [])
        if str(cid) not in subs:
            subs.append(str(cid))
            save_json(SUBS_FILE, subs)

        m1 = await context.bot.send_message(cid,
            "🚀 NƠI BÓNG TỐI BẮT ĐẦU... NƠI BẢN NĂNG THỨC TỈNH 🚀")
        await asyncio.sleep(1)

        m2 = await context.bot.send_message(cid,
            "👿 KHÔNG DÀNH CHO NGƯỜI YẾU TIM - CHỈ DÀNH CHO KẺ DÁM KHÁM PHÁ 👿")
        await asyncio.sleep(1)

        m3 = await context.bot.send_message(cid,
            "👀 BƯỚC VÀO ĐÂY BẠN SẼ KHÔNG MUỐN QUAY LẠI 👀")
        await asyncio.sleep(2)

        for m in [m1, m2, m3]:
            try:
                await context.bot.delete_message(cid, m.message_id)
            except:
                pass

        keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="albums")]]
        await context.bot.send_message(cid,
            "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def show_albums(update, context):
        query = update.callback_query
        await query.answer()

        albums = load_json(ALBUMS_FILE, {})

        buttons = []
        for k in albums:
            date = k.replace("album_", "").replace("_","-")
            buttons.append([InlineKeyboardButton(f"Link🔥 {date}", callback_data=k)])

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])

        await query.message.edit_text("📂 Chọn album:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def open_album(update, context):
        query = update.callback_query
        await query.answer()

        album_id = query.data
        albums = load_json(ALBUMS_FILE, {})

        if album_id not in albums:
            return

        media = []
        for i, img in enumerate(albums[album_id]):
            media.append(InputMediaPhoto(
                media=img["url"],
                caption=img.get("caption","") if i == 0 else None
            ))

        for i in range(0, len(media), 10):
            await context.bot.send_media_group(
                chat_id=query.message.chat_id,
                media=media[i:i+10]
            )

    async def back(update, context):
        query = update.callback_query
        await query.answer()

        keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="albums")]]
        await query.message.edit_text(
            "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    app_bot = Application.builder().token(BOT_TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(show_albums, pattern="albums"))
    app_bot.add_handler(CallbackQueryHandler(back, pattern="back"))
    app_bot.add_handler(CallbackQueryHandler(open_album))

    return app_bot

# ===== RUN BOT =====
def run_bot():
    async def main():
        app_bot = build_app()

        await app_bot.initialize()
        await app_bot.start()

        log.info("✅ Bot running")

        await asyncio.Event().wait()

    asyncio.run(main())

# ===== MAIN =====
if __name__ == "__main__":
    log.info("🚀 Server starting...")

    threading.Thread(target=run_bot, daemon=True).start()

    app.run(host="0.0.0.0", port=5000)

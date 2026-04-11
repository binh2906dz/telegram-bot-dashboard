import os
import json
import asyncio
import threading
from datetime import datetime

from flask import Flask, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===== CONFIG =====
TOKEN = "8448093514:AAGK58_UCuF2YK34YghvURfvX0YD5BZ0t1g"
ADMIN_CHAT_ID = 5914285286

app = Flask(__name__)

# ===== FILE SAFE =====
def load(file, default):
    try:
        with open(file, "r") as f:
            return json.load(f)
    except:
        return default

def save(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

# create files if missing
for f, d in [
    ("subscribers.json", []),
    ("albums.json", {}),
]:
    if not os.path.exists(f):
        save(f, d)

# ===== DASHBOARD =====
@app.route("/")
def home():
    subs = load("subscribers.json", [])
    albums = load("albums.json", {})

    return render_template_string(f"""
    <h1>🚀 Telegram Dashboard</h1>
    <p>Subscribers: {len(subs)}</p>
    <p>Albums: {len(albums)}</p>
    """)

# ===== TELEGRAM =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    subs = load("subscribers.json", [])
    if chat_id not in subs:
        subs.append(chat_id)
        save("subscribers.json", subs)

    # intro
    m1 = await update.message.reply_text(
        "🚀 NƠI BÓNG TỐI BẮT ĐẦU... NƠI BẢN NĂNG THỨC TỈNH 🚀"
    )
    await asyncio.sleep(1)

    m2 = await update.message.reply_text(
        "👿 KHÔNG DÀNH CHO NGƯỜI YẾU TIM - CHỈ DÀNH CHO KẺ DÁM KHÁM PHÁ 👿"
    )
    await asyncio.sleep(1)

    m3 = await update.message.reply_text(
        "👀 BƯỚC VÀO ĐÂY BẠN SẼ KHÔNG MUỐN QUAY LẠI 👀"
    )
    await asyncio.sleep(2)

    # delete
    for m in [m1, m2, m3]:
        try:
            await context.bot.delete_message(chat_id, m.message_id)
        except:
            pass

    # final
    keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="albums")]]
    await update.message.reply_text(
        "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def show_albums(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    albums = load("albums.json", {})

    if not albums:
        await query.message.reply_text("Chưa có album")
        return

    buttons = []
    for k in albums:
        date = k.replace("album_", "").replace("_", "-")
        buttons.append([InlineKeyboardButton(f"Link🔥 {date}", callback_data=k)])

    await query.message.reply_text(
        "📂 Danh sách album:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    albums = load("albums.json", {})
    album_id = query.data

    if album_id not in albums:
        return

    for item in albums[album_id]:
        await context.bot.send_message(query.message.chat_id, item["url"])

# ===== BOT THREAD =====
def run_bot():
    async def main():
        app_bot = ApplicationBuilder().token(TOKEN).build()

        app_bot.add_handler(CommandHandler("start", start))
        app_bot.add_handler(CallbackQueryHandler(show_albums, pattern="albums"))
        app_bot.add_handler(CallbackQueryHandler(send_album))

        print("✅ Bot starting...")

        await app_bot.initialize()
        await app_bot.start()

        print("✅ Bot running")

        # gửi link dashboard cho admin
        try:
            domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
            if domain:
                url = f"https://{domain}"
                await app_bot.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"🌐 Dashboard của bạn:\n{url}",
                )
        except Exception as e:
            print("Send link error:", e)

        # giữ bot sống
        await asyncio.Event().wait()

    asyncio.run(main())

# ===== MAIN =====
if __name__ == "__main__":
    print("🚀 Server starting...")

    threading.Thread(target=run_bot, daemon=True).start()

    app.run(host="0.0.0.0", port=5000)

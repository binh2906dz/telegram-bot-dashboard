import os
import json
import threading
from datetime import datetime
from flask import Flask, request, redirect

# ===== CONFIG =====
BOT_TOKEN = "8448093514:AAGK58_UCuF2YK34YghvURfvX0YD5BZ0t1g"
ADMIN_ID = 5914285286

# ===== FILE =====
SUBS_FILE = "subscribers.json"
ALBUMS_FILE = "albums.json"

def load_json(file, default):
    try:
        if os.path.exists(file):
            with open(file, "r") as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

for f, d in [(SUBS_FILE, []), (ALBUMS_FILE, {})]:
    if not os.path.exists(f):
        save_json(f, d)

# ===== FLASK =====
app = Flask(__name__)

@app.route("/")
def home():
    return "🚀 Bot đang chạy"

# ===== WEB ADMIN =====
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        urls = request.form.get("urls", "").split("\n")
        captions = request.form.get("captions", "").split("\n")

        today = datetime.now().strftime("%Y_%m_%d")
        album_id = f"album_{today}"

        albums = load_json(ALBUMS_FILE, {})

        if album_id not in albums:
            albums[album_id] = []

        for i, url in enumerate(urls):
            if url.strip():
                albums[album_id].append({
                    "url": url.strip(),
                    "caption": captions[i] if i < len(captions) else ""
                })

        save_json(ALBUMS_FILE, albums)
        return redirect("/upload")

    return """
    <h2>UPLOAD LINK HOT</h2>
    <form method="post">
    <textarea name="urls" placeholder="Mỗi link 1 dòng"></textarea><br>
    <textarea name="captions" placeholder="Caption"></textarea><br>
    <button>UPLOAD</button>
    </form>
    """

# ===== TELEGRAM =====
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

def build_bot():

    async def start(update, context):
        cid = update.effective_chat.id

        subs = load_json(SUBS_FILE, [])
        if str(cid) not in subs:
            subs.append(str(cid))
            save_json(SUBS_FILE, subs)

        await context.bot.send_chat_action(chat_id=cid, action="typing")

        m1 = await context.bot.send_message(cid,
            "🚀 NƠI BÓNG TỐI BẮT ĐẦU... NƠI BẢN NĂNG THỨC TỈNH 🚀")
        await context.application.create_task(context.bot.send_message(cid,
            "👿 KHÔNG DÀNH CHO NGƯỜI YẾU TIM - CHỈ DÀNH CHO KẺ DÁM KHÁM PHÁ 👿"))
        await context.application.create_task(context.bot.send_message(cid,
            "👀 BƯỚC VÀO ĐÂY BẠN SẼ KHÔNG MUỐN QUAY LẠI 👀"))

        keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="albums")]]

        await context.bot.send_message(
            cid,
            "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def show_albums(update, context):
        query = update.callback_query
        await query.answer()

        albums = load_json(ALBUMS_FILE, {})

        if not albums:
            await query.message.reply_text("Chưa có album")
            return

        buttons = []
        for k in sorted(albums.keys(), reverse=True):
            date = k.replace("album_", "").replace("_", "-")
            buttons.append([InlineKeyboardButton(f"Link🔥 {date}", callback_data=k)])

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])

        await query.message.edit_text(
            "📂 Chọn album:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def open_album(update, context):
        query = update.callback_query
        await query.answer()

        albums = load_json(ALBUMS_FILE, {})
        album_id = query.data

        if album_id not in albums:
            return

        media = []
        for i, img in enumerate(albums[album_id]):
            media.append(InputMediaPhoto(
                media=img["url"],
                caption=img.get("caption", "") if i == 0 else None
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

# ===== FIX CHÍNH Ở ĐÂY =====
def run_bot():
    bot = build_bot()
    bot.run_polling()  # 🔥 FIX LỖI BOT IM

# ===== MAIN =====
if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)

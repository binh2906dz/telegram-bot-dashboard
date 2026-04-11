import json, os, asyncio, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

def load_json(file, default):
    try:
        with open(file) as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

# ================= START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id

    subs = load_json("subscribers.json", [])
    if user_id not in subs:
        subs.append(user_id)
        save_json("subscribers.json", subs)

    m1 = await update.message.reply_text("🚀 NƠI BÓNG TỐI BẮT ĐẦU... NƠI BẢN NĂNG THỨC TỈNH 🚀")
    await asyncio.sleep(1)
    m2 = await update.message.reply_text("👿 KHÔNG DÀNH CHO NGƯỜI YẾU TIM - CHỈ DÀNH CHO KẺ DÁM KHÁM PHÁ 👿")
    await asyncio.sleep(1)
    m3 = await update.message.reply_text("👀 BƯỚC VÀO ĐÂY BẠN SẼ KHÔNG MUỐN QUAY LẠI 👀")

    await asyncio.sleep(2)
    for m in [m1, m2, m3]:
        try:
            await m.delete()
        except:
            pass

    keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
    await update.message.reply_text("NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏", reply_markup=InlineKeyboardMarkup(keyboard))

# ================= MENU =================
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    albums = load_json("albums.json", {})
    buttons = []

    for key in sorted(albums.keys()):
        date = key.replace("album_", "").replace("_", "-")
        buttons.append([InlineKeyboardButton(f"Link🔥 {date[8:10]}-{date[5:7]}", callback_data=key)])

    buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])

    await query.edit_message_text("Chọn album:", reply_markup=InlineKeyboardMarkup(buttons))

# ================= SEND ALBUM =================
async def send_album(chat_id, context, album):
    media = []
    for i, p in enumerate(album["photos"]):
        if i == 0:
            media.append(InputMediaPhoto(p["url"], caption=p.get("caption", "")))
        else:
            media.append(InputMediaPhoto(p["url"]))

    await context.bot.send_media_group(chat_id, media)

async def album_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    albums = load_json("albums.json", {})
    album = albums.get(query.data)

    await query.delete_message()

    if album:
        await send_album(query.message.chat_id, context, album)

# ================= SET TIME =================
async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return

    try:
        time_str = update.message.text.split("_")[1]
        h, m = map(int, time_str.split(":"))

        h_utc = (h - 7) % 24

        save_json("config.json", {"hour": h_utc, "minute": m})

        await update.message.reply_text(f"Đã set giờ: {h}:{m} (VN)")
    except:
        await update.message.reply_text("Sai format")

# ================= SCHEDULER =================
last_sent = None

async def scheduler(app):
    global last_sent
    while True:
        now = datetime.datetime.utcnow()
        cfg = load_json("config.json", {"hour": 0, "minute": 0})

        if now.hour == cfg["hour"] and now.minute == cfg["minute"]:
            key = f"{now.hour}:{now.minute}"
            if last_sent != key:
                last_sent = key

                albums = load_json("albums.json", {})
                if not albums:
                    continue

                latest = sorted(albums.keys())[-1]
                subs = load_json("subscribers.json", [])

                success, fail = 0, 0

                for u in subs:
                    try:
                        await send_album(u, app.bot, albums[latest])
                        success += 1
                    except:
                        fail += 1

                await app.bot.send_message(ADMIN_ID,
                    f"📊 Report\nUsers: {len(subs)}\nOK: {success}\nFail: {fail}"
                )

        await asyncio.sleep(30)

# ================= RUN =================
def run_bot():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settudongguilink", set_time))
    app.add_handler(CallbackQueryHandler(menu, pattern="menu"))
    app.add_handler(CallbackQueryHandler(album_click))

    app.job_queue.run_once(lambda ctx: asyncio.create_task(scheduler(app)), 1)

    app.run_polling() 

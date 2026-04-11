import logging
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ====== CONFIG ======
BOT_TOKEN = "8448093514:AAGK58_UCuF2YK34YghvURfvX0YD5BZ0t1g"
BASE_URL = "https://telegram-bot-dashboard-production-e769.up.railway.app"

# ====== LOG ======
logging.basicConfig(level=logging.INFO)

# ====== FLASK ======
app = Flask(__name__)

# ====== TELEGRAM APP ======
bot_app = Application.builder().token(BOT_TOKEN).build()

# ====== START COMMAND ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔥 VÀO DASHBOARD", url=BASE_URL)],
        [InlineKeyboardButton("💎 KIẾM TIỀN NGAY", url=BASE_URL)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🔥 Chào mừng bạn đến hệ thống VIP\n\n👉 Nhấn nút bên dưới để bắt đầu:",
        reply_markup=reply_markup
    )

bot_app.add_handler(CommandHandler("start", start))

# ====== WEBHOOK ======
@app.route("/webhook", methods=["POST"])
async def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return "OK"

# ====== SET WEBHOOK ======
@app.route("/setwebhook")
async def set_webhook():
    await bot_app.bot.set_webhook(f"{BASE_URL}/webhook")
    return "Webhook set!"

# ====== HOME ======
@app.route("/")
def home():
    return "Bot is running!"

# ====== RUN ======
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

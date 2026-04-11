import threading
from flask import Flask
import telebot

# ====== CONFIG (ĐÃ GẮN SẴN) ======
TOKEN = "8448093514:AAGK58_UCuF2YK34YghvURfvX0YD5BZ0t1g"
ADMIN_ID = 5914285286

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ====== DOMAIN ======
DOMAIN = "https://telegram-bot-dashboard-production-e769.up.railway.app"

# ====== WEB ======
@app.route("/")
def home():
    return f"""
    <h1>🚀 Telegram Dashboard</h1>
    <p>Bot đang hoạt động ✅</p>
    <p><a href="{DOMAIN}">{DOMAIN}</a></p>
    """

# ====== TELEGRAM ======
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.chat.id

    bot.send_message(user_id, "🔥 Chào mừng bạn đến hệ thống VIP")
    bot.send_message(user_id, "👉 Nhấn link dưới để vào dashboard")

    bot.send_message(
        user_id,
        f"🌐 Dashboard của bạn:\n{DOMAIN}"
    )

# ====== RUN BOT ======
def run_bot():
    print("✅ Bot started")
    bot.infinity_polling()

# ====== MAIN ======
if __name__ == "__main__":
    threading.Thread(target=run_bot).start()

    app.run(host="0.0.0.0", port=5000)

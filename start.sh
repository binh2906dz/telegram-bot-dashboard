#!/bin/bash
set -e

echo "=== VÀO SCRIPT KHỞI ĐỘNG ==="
echo "PORT đang được gán là: ${PORT:-8080}"

# Khởi chạy Telegram bot dưới nền
echo "Đang khởi chạy Telegram Bot..."
python run_bot.py &
BOT_PID=$!
echo "Bot đã chạy với PID: $BOT_PID"

# Khởi chạy Flask web server với Gunicorn ở foreground
echo "Đang khởi chạy Gunicorn Web Server..."
gunicorn app:app -b 0.0.0.0:${PORT:-8080} --workers 1 --timeout 120 --log-level info

echo "Gunicorn đã dừng, chờ Bot tiến trình..."
wait $BOT_PID

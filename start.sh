#!/bin/bash
set -e

echo "Starting bot..."
python run_bot.py &
BOT_PID=$!
echo "Bot started with PID: $BOT_PID"

echo "PORT is ${PORT:-8080}"
echo "Starting gunicorn..."
gunicorn app:app -b 0.0.0.0:${PORT:-8080} --workers 1 --timeout 120 --log-level info

wait

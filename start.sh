#!/bin/bash
set -e

echo "=== STARTING APP ==="
export PORT="${PORT:-8080}"
echo "Using PORT: $PORT"

echo "Starting Telegram Bot in background..."
python run_bot.py &

echo "Starting Gunicorn Web Server..."
exec gunicorn app:app -b "0.0.0.0:$PORT" --workers 1 --timeout 120 --log-level info
#!/bin/bash
# PaaS-style entry (Railway, etc.): background ``python run_bot.py`` + Gunicorn on PORT.
# Ubuntu VPS: prefer systemd + ``gunicorn app:app`` only (see setup_vps.sh).  Running
# both this script and the VPS service can duplicate bot logic — avoid on production VPS.
set -e

echo "=== STARTING APP ==="
export PORT="${PORT:-5000}"
echo "Using PORT: $PORT"

echo "Starting Telegram Bot in background..."
python run_bot.py &

echo "Starting Gunicorn Web Server..."
exec gunicorn app:app -b "0.0.0.0:$PORT" --workers 1 --timeout 120 --log-level info
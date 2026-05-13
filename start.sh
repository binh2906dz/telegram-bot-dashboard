#!/bin/bash
set -e

echo "=== STARTING APP ==="
export PORT="${PORT:-8080}"
echo "Using PORT: $PORT"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "./venv/bin/python" ]; then
    PYTHON_BIN="./venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "Python interpreter not found. Please install Python or create ./venv." >&2
    exit 1
  fi
fi

echo "Starting Telegram Bot in background..."
"$PYTHON_BIN" run_bot.py &

if [ -x "./venv/bin/gunicorn" ]; then
  GUNICORN_BIN="./venv/bin/gunicorn"
elif command -v gunicorn >/dev/null 2>&1; then
  GUNICORN_BIN="gunicorn"
else
  echo "Gunicorn not found. Please install dependencies first." >&2
  exit 1
fi

echo "Starting Gunicorn Web Server..."
exec "$GUNICORN_BIN" app:app -b "0.0.0.0:$PORT" --workers 1 --timeout 120 --log-level info
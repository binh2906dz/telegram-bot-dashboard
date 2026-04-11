#!/bin/bash
set -e

# Start the Telegram bot in the background
python run_bot.py &

# Start the Flask web server using Gunicorn in the foreground
exec gunicorn app:app -b 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

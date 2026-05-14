"""Optional bot runner used by start.sh / some PaaS Procfiles.

Typical Ubuntu VPS + systemd uses only ``gunicorn app:app`` (BotManager starts
inside the worker).  Do not enable this script there unless you intend a second
bot process — it can fight over the same bot-manager lock / SQLite.
"""
import time
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_runner")

if __name__ == "__main__":
    log.info("Starting standalone bot process...")
    from app import run_bot_thread

    while True:
        try:
            log.info("Starting run_bot_thread()...")
            run_bot_thread()
        except Exception as e:
            log.exception(f"Bot process crashed: {e}")

        log.info("Bot thread exited. Restarting in 5 seconds...")
        time.sleep(5)

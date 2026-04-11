import time
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_runner")

if __name__ == "__main__":
    log.info("Starting standalone bot process...")
    from app import run_bot_thread

    while True:
        try:
            run_bot_thread()
        except Exception as e:
            log.exception(f"Bot process crashed: {e}")
            log.info("Restarting bot in 5 seconds...")
            time.sleep(5)

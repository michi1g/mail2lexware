import logging
import threading
import time

from . import config, service

log = logging.getLogger("mail2lexware")


def _loop() -> None:
    next_run = time.time() + 60  # erster Durchlauf kurz nach dem Start
    while True:
        time.sleep(15)
        minutes = int(config.load_settings()["interval_minutes"])
        if minutes <= 0:
            next_run = time.time() + 60
            continue
        if time.time() >= next_run:
            result = service.run_cycle()
            log.info("Durchlauf: %s", result)
            next_run = time.time() + minutes * 60


def start() -> None:
    threading.Thread(target=_loop, name="scheduler", daemon=True).start()

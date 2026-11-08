"""Translate queued, redacted USCIS wording without blocking web requests."""
import logging
import time
from app.google_translation import process_one

log = logging.getLogger("translation")

def main():
    logging.basicConfig(level=logging.WARNING)
    while True:
        try:
            state = process_one()
            time.sleep(1 if state in ("ready", "failed", "queued", "budget") else 5)
        except Exception as exc:
            # No exception values: provider errors may contain sensitive request details.
            log.warning("Translation worker recovered: %s", type(exc).__name__)
            time.sleep(15)

if __name__ == "__main__":
    main()

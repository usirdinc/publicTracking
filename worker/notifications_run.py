"""Deliver queued staff-verified result changes. Never perform agency lookups."""
import logging,time
from app.staff_notifications import drain_one

def main():
    while True:
        try:time.sleep(1 if drain_one() else 5)
        except Exception as exc:
            logging.warning("Notification worker recovered: %s",type(exc).__name__)
            time.sleep(15)

if __name__=='__main__':main()

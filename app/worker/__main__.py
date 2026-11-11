"""Run with python -m app.worker; demo stages always require an explicit choice."""
import argparse
import logging
from pathlib import Path
import signal
import sqlite3
import sys
import time

from app.config import Settings
from app.storage import Storage, StorageError
from app.worker.service import WorkerService
from app.workflow.stages import RecordingSink, load_registry


class StopFlag:
    """No locks in a Python signal handler; idle shutdown waits at most one poll."""
    requested = False

    def set(self):
        self.requested = True

    def is_set(self):
        return self.requested

    def wait(self, timeout):
        if not self.requested:
            time.sleep(timeout)


def main():
    parser = argparse.ArgumentParser(description="Persistent strategy worker")
    parser.add_argument("--stages", help="demo or a trusted package.module:build_stages factory")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--once", action="store_true", help="Drain currently eligible runs and exit")
    args = parser.parse_args()
    try:
        settings = Settings(**({"data_dir": args.data_dir} if args.data_dir else {}))
        storage = Storage(settings.data_dir)
        registry = load_registry(args.stages or settings.stage_factory, settings, RecordingSink(storage.record))
    except Exception:
        print("Worker configuration failed. Set --stages demo or APP_STAGE_FACTORY=package.module:build_stages; check APP_DATA_DIR.", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level.upper(), format="%(levelname)s %(message)s")
    stop = StopFlag()
    # Set a flag only; external-action guards and stage boundaries observe it.
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda signum, frame: stop.set())
    try:
        WorkerService(storage, registry, poll_seconds=settings.worker_poll_seconds).run(once=args.once, stop=stop)
    except StorageError as error:
        print("Worker stopped: " + str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("Worker stopped because persistent storage is unavailable; no next step was started.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

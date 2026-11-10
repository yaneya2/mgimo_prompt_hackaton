"""Queue consumer independent of the HTTP server and UI lifecycle."""
import logging
from threading import Event

from app.workflow import Orchestrator

logger = logging.getLogger(__name__)


class WorkerService:
    def __init__(self, storage, registry, *, poll_seconds=0.5):
        if not 0 < poll_seconds <= 10:
            raise ValueError("Worker polling interval must be in (0, 10]")
        self.storage = storage
        self.engine = Orchestrator(storage, registry)
        self.poll_seconds = poll_seconds

    def run(self, *, once=False, stop=None):
        """once drains eligible runs, stopping each at completion or an input boundary."""
        stop = Event() if stop is None else stop
        processed = 0
        with self.storage.worker() as worker:
            logger.info("Worker started; recovered=%d synthetic=%s", len(worker.recovered_run_ids), self.engine.registry.synthetic)
            while not stop.is_set():
                run = self.storage.next_queued_run()
                if run is None:
                    if once:
                        break
                    stop.wait(self.poll_seconds)
                    continue
                result = self.engine.execute(worker, run["id"], stop)
                processed += 1
                logger.info("Run %s status=%s stage=%s", result["id"], result["status"], result["stage"])
        return processed

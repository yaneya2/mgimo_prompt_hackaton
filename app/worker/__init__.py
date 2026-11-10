"""Persistent queue worker; run separately with python -m app.worker."""
from app.worker.service import WorkerService

__all__ = ["WorkerService"]

"""Celery app. Runs in eager mode by default for dev (no broker needed)."""
from celery import Celery

from app.config import settings

celery_app = Celery(
    "leasegenie",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
)

# Import tasks so Celery registers them
from app.workers import tasks  # noqa: E402, F401

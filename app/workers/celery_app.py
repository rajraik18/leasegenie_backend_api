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
    # Reliability — survive worker crashes and don't stall the queue forever.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    # Bound result-backend growth.
    result_expires=settings.celery_result_expires_seconds,
    # Don't prefetch lots of tasks per worker — keeps long extractions from
    # being held behind a busy worker.
    worker_prefetch_multiplier=1,
)

# Import tasks so Celery registers them
from app.workers import tasks  # noqa: E402, F401

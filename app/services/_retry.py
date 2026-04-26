"""Tiny in-house retry helper.

Used in `app/agents/ollama_client.py::_retry_call` for LLM calls and in
`app/services/vector_store.py` for pgvector raw-SQL operations. Keeps the
pattern uniform without taking a dep on `tenacity`.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


# Network / transient errors we'll retry. Logical SQLAlchemy errors (e.g.
# IntegrityError, ProgrammingError) bubble up immediately.
def _is_transient(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    # SQLAlchemy / psycopg2 disconnects.
    return name in {
        "OperationalError",
        "InterfaceError",
        "DBAPIError",
    }


def with_backoff(
    fn: Callable[[], T],
    *,
    label: str,
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    backoff: float = 2.0,
) -> T:
    """Run `fn`; retry transient failures up to `max_attempts` times with
    exponential back-off (`initial_delay`, `initial_delay*backoff`, ...).

    Raises the last exception if all attempts fail. Non-transient errors
    are raised immediately on the first attempt.
    """
    delay = max(0.0, initial_delay)
    last_exc: BaseException | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return fn()
        except BaseException as exc:
            if not _is_transient(exc) or attempt == max_attempts:
                raise
            last_exc = exc
            logger.warning(
                "%s attempt %d/%d failed: %s (%s) -- retrying in %.2fs",
                label, attempt, max_attempts, exc, exc.__class__.__name__, delay,
            )
            time.sleep(delay)
            delay *= backoff
    # Defensive — should never reach here.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label} produced no result")

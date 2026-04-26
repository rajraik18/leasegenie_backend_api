"""Per-IP rate limiter (slowapi).

Two limits are applied via SlowAPIMiddleware:

  - `RATE_LIMIT_DEFAULT` (default 100/minute) -- broad anti-abuse cap on
    every endpoint.
  - `RATE_LIMIT_EXTRACT` (default 10/hour) -- stricter cap that fires
    only on requests whose path starts with `/api/v1/extract/`. Selected
    via the `key_func` so we can label the bucket separately.

Operators with stricter needs (per-tenant quotas, sliding windows, etc.)
should configure them at the reverse proxy -- see
`deploy/windows/REVERSE_PROXY.md` for Caddy / IIS / nginx examples.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def _key_func(request) -> str:
    """Bucket key. Adds an `extract:` prefix on the expensive endpoint so
    `RATE_LIMIT_EXTRACT` can apply only there while `RATE_LIMIT_DEFAULT`
    governs everything else."""
    ip = get_remote_address(request)
    if request.url.path.startswith("/api/v1/extract/"):
        return f"extract:{ip}"
    return ip


limiter = Limiter(
    key_func=_key_func,
    default_limits=[settings.rate_limit_default],
    application_limits=[
        # Anything that hits the `extract:` bucket also gets the stricter
        # extract limit. Paths that don't start with /api/v1/extract/ never
        # see this bucket, so the limit is effectively scoped.
        f"{settings.rate_limit_extract}",
    ],
)

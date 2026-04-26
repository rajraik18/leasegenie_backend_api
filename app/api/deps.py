"""Shared FastAPI dependencies."""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db


def require_api_key(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Reject the request unless the caller presents the configured API key.

    Accepts either:
        Authorization: Bearer <key>
        X-API-Key: <key>

    When `settings.api_key` is None the dependency is a no-op -- this is the
    expected configuration ONLY for local development. Production deployments
    must set `API_KEY` in `.env` so this dependency starts enforcing.
    """
    if settings.api_key is None:
        return

    presented: str | None = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            presented = value.strip()
    if presented is None and x_api_key:
        presented = x_api_key.strip()

    if presented is None or not secrets.compare_digest(presented, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = ["get_db", "Depends", "Session", "require_api_key"]

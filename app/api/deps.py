"""Shared FastAPI dependencies."""
from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db

__all__ = ["get_db", "Depends", "Session"]

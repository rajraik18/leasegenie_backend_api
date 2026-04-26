"""baseline -- captures schema as of v3.0

The schema for every table already exists in scripts/db/schema.sql, which
is the canonical source for fresh installs. This baseline migration is
deliberately a NO-OP: it just stamps the Alembic version table so future
migrations have a parent revision to chain off.

Workflow for the next schema change:
    .venv\\Scripts\\python -m alembic revision --autogenerate -m "<message>"
    # review the generated file in alembic/versions/
    .\\scripts\\db.ps1 alembic-upgrade

Revision ID: 0001
Revises:
Create Date: 2026-04-26 00:00:00
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op. Existing deployments already have the v3.0 schema."""
    pass


def downgrade() -> None:
    """No-op. Baseline cannot be downgraded."""
    pass

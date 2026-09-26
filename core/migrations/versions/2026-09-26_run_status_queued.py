"""analysis_run.status: the initial status is "queued" (canonical run.status, ARCHITECTURE §4.7), not "pending"

Revision ID: 2026_09_26_run_status_queued
Revises: 2026_09_25_init
Create Date: 2026-09-26 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "2026_09_26_run_status_queued"
down_revision: str | None = "2026_09_25_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("analysis_run", "status", server_default="queued", schema="core")
    op.execute("UPDATE core.analysis_run SET status = 'queued' WHERE status = 'pending'")
    op.execute("UPDATE core.run_event SET status = 'queued' WHERE stage = 'run' AND status = 'pending'")


def downgrade() -> None:
    op.execute("UPDATE core.run_event SET status = 'pending' WHERE stage = 'run' AND status = 'queued'")
    op.execute("UPDATE core.analysis_run SET status = 'pending' WHERE status = 'queued'")
    op.alter_column("analysis_run", "status", server_default="pending", schema="core")

"""domain_event delivery bookkeeping for the outbox dispatcher

Revision ID: 2026_09_26_event_delivery
Revises: 2026_09_25_init
Create Date: 2026-09-26 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "2026_09_26_event_delivery"
down_revision: str | None = "2026_09_25_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "domain_event",
        sa.Column("delivered_to", ARRAY(sa.String()), server_default="{}", nullable=False),
        schema="core",
    )
    op.add_column(
        "domain_event",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        schema="core",
    )
    op.add_column("domain_event", sa.Column("last_error", sa.Text(), nullable=True), schema="core")
    op.create_index("idx_domain_event_org_created", "domain_event", ["org_id", "created_at"], schema="core")


def downgrade() -> None:
    op.drop_index("idx_domain_event_org_created", table_name="domain_event", schema="core")
    op.drop_column("domain_event", "last_error", schema="core")
    op.drop_column("domain_event", "attempts", schema="core")
    op.drop_column("domain_event", "delivered_to", schema="core")

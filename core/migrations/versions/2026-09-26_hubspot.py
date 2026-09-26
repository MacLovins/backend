"""HubSpot push: the HubSpot record id of a company and when it was last pushed.

Revision ID: 2026_09_26_hubspot
Revises: 2026_09_26_outreach_job
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026_09_26_hubspot"
down_revision: str | None = "2026_09_26_outreach_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("company", sa.Column("hubspot_company_id", sa.String(32), nullable=True), schema="core")
    op.add_column(
        "company", sa.Column("hubspot_synced_at", sa.DateTime(timezone=True), nullable=True), schema="core"
    )


def downgrade() -> None:
    op.drop_column("company", "hubspot_synced_at", schema="core")
    op.drop_column("company", "hubspot_company_id", schema="core")

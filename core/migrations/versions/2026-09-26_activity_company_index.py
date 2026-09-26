"""domain_event: index for GET /activity?company_id= (payload->>'company_id')

Revision ID: 2026_09_26_activity_company_idx
Revises: 2026_09_26_outreach_job
Create Date: 2026-09-26 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "2026_09_26_activity_company_idx"
down_revision: str | None = "2026_09_26_outreach_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_domain_event_org_company_created "
        "ON core.domain_event (org_id, (payload->>'company_id'), created_at)"
    )


def downgrade() -> None:
    op.drop_index("idx_domain_event_org_company_created", table_name="domain_event", schema="core")

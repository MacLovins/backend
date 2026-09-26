"""alert_rule.email_frequency (instant or a digest) and the signal's category and strength on notifications

Revision ID: 2026_09_27_alert_digest
Revises: 2026_09_27_question_temperature
Create Date: 2026-09-27 09:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026_09_27_alert_digest"
down_revision: str | None = "2026_09_27_question_temperature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# existing signal notifications take category and strength from their event while it is still kept
BACKFILL_SQL = """
UPDATE core.notification AS n
SET category = e.payload->>'category',
    strength = CASE WHEN e.payload->>'strength' IN ('weak', 'moderate', 'strong')
                    THEN e.payload->>'strength' END
FROM core.domain_event AS e
WHERE n.event_id = e.id AND n.kind = 'signal'
"""


def upgrade() -> None:
    op.add_column(
        "alert_rule",
        sa.Column("email_frequency", sa.String(16), server_default="instant", nullable=False),
        schema="core",
    )
    op.add_column("notification", sa.Column("category", sa.String(64), nullable=True), schema="core")
    op.add_column("notification", sa.Column("strength", sa.String(16), nullable=True), schema="core")
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_column("notification", "strength", schema="core")
    op.drop_column("notification", "category", schema="core")
    op.drop_column("alert_rule", "email_frequency", schema="core")

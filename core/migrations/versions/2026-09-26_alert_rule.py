"""alert_rule: alerts users configure themselves (scope + trigger, in-app and/or e-mail)

Revision ID: 2026_09_26_alert_rule
Revises: 2026_09_26_activity_company_idx
Create Date: 2026-09-26 12:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "2026_09_26_alert_rule"
down_revision: str | None = "2026_09_26_activity_company_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_rule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("channels", ARRAY(sa.String()), server_default="{inapp}", nullable=False),
        sa.Column("scope", JSONB(), server_default="{}", nullable=False),
        sa.Column("trigger", JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index("idx_alert_rule_org_active", "alert_rule", ["org_id", "is_active"], schema="core")
    op.create_index("idx_alert_rule_user", "alert_rule", ["user_id"], schema="core")


def downgrade() -> None:
    op.drop_index("idx_alert_rule_user", table_name="alert_rule", schema="core")
    op.drop_index("idx_alert_rule_org_active", table_name="alert_rule", schema="core")
    op.drop_table("alert_rule", schema="core")

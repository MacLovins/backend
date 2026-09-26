"""notification: in-app notifications produced by alert rules (with the e-mail delivery outcome)

Revision ID: 2026_09_26_notification
Revises: 2026_09_26_alert_rule
Create Date: 2026-09-26 12:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "2026_09_26_notification"
down_revision: str | None = "2026_09_26_alert_rule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "rule_id", sa.Uuid(), sa.ForeignKey("core.alert_rule.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column(
            "company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("trend_kind", sa.String(32), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered", JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index(
        "idx_notification_user_created", "notification", ["user_id", "created_at"], schema="core"
    )
    op.create_index(
        "idx_notification_user_unread",
        "notification",
        ["user_id"],
        schema="core",
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "idx_notification_rule_company_created",
        "notification",
        ["rule_id", "company_id", "created_at"],
        schema="core",
    )
    op.create_index("idx_notification_rule_event", "notification", ["rule_id", "event_id"], schema="core")


def downgrade() -> None:
    op.drop_index("idx_notification_rule_event", table_name="notification", schema="core")
    op.drop_index("idx_notification_rule_company_created", table_name="notification", schema="core")
    op.drop_index("idx_notification_user_unread", table_name="notification", schema="core")
    op.drop_index("idx_notification_user_created", table_name="notification", schema="core")
    op.drop_table("notification", schema="core")

"""signal_question.temperature: the question's own cold / medium / hot guide for signal strength

Revision ID: 2026_09_27_question_temperature
Revises: 2026_09_26_hubspot
Create Date: 2026-09-27 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "2026_09_27_question_temperature"
down_revision: str | None = "2026_09_26_hubspot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signal_question", sa.Column("temperature", JSONB(), nullable=True), schema="core")


def downgrade() -> None:
    op.drop_column("signal_question", "temperature", schema="core")

"""signal.evidence_key: user rejections survive re-analysis

Revision ID: 2026_09_26_evidence_key
Revises: 2026_09_26_event_delivery
Create Date: 2026-09-26 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026_09_26_evidence_key"
down_revision: str | None = "2026_09_26_event_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# same normalisation as leadradar_core.modules.intelligence.evidence.evidence_key (parity is tested)
BACKFILL_SQL = r"""
UPDATE core.signal SET evidence_key = encode(sha256(convert_to(
    question_key || E'\n'
    || regexp_replace(lower(btrim(quote, E' \t\n\r\f\v')), '\s+', ' ', 'g') || E'\n'
    || lower(rtrim(btrim(split_part(coalesce(url, ''), '#', 1), E' \t\n\r\f\v'), '/')),
    'UTF8')), 'hex')
WHERE evidence_key IS NULL
"""


def upgrade() -> None:
    op.add_column("signal", sa.Column("evidence_key", sa.String(64), nullable=True), schema="core")
    op.execute(BACKFILL_SQL)
    op.create_index(
        "idx_signal_evidence_key",
        "signal",
        ["company_id", "service_id", "evidence_key"],
        schema="core",
    )


def downgrade() -> None:
    op.drop_index("idx_signal_evidence_key", table_name="signal", schema="core")
    op.drop_column("signal", "evidence_key", schema="core")

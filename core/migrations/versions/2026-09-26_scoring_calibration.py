"""Scoring calibration: tau_intent 3 → 5, intent_exponent 0.6 → 0.8 in stored profiles that still hold the old
defaults. Profiles an admin tuned are left alone. New parameters (hot_min_questions, hot_min_strength,
undated_decay) need no data change: missing keys take the engine defaults.

Revision ID: 2026_09_26_scoring
Revises: 2026_09_25_init
"""

from collections.abc import Sequence

from alembic import op

revision: str = "2026_09_26_scoring"
down_revision: str | None = "2026_09_25_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _swap(old_tau: int, old_exp: float, new_tau: int, new_exp: float) -> None:
    op.execute(
        f"""
        UPDATE core.scoring_profile
        SET params = params || '{{"tau_intent": {new_tau}, "intent_exponent": {new_exp}}}'::jsonb,
            updated_at = now()
        WHERE (params->>'tau_intent')::float = {old_tau}
          AND (params->>'intent_exponent')::float = {old_exp}
        """
    )


def upgrade() -> None:
    _swap(3, 0.6, 5, 0.8)


def downgrade() -> None:
    _swap(5, 0.8, 3, 0.6)

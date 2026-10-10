"""Retention: a run whose detail was pruned says so, and the model cache finds its call fast.

`sourcing_run.pruned_at` marks a run whose run-scoped rows (its listings, candidates, pro-formas,
signals, narratives, brief and delivery ledger) were deleted after the retention window; the run's
own row stays, with its counts and error. `ix_llm_result_llm_call_id` closes a known gap: deleting
an old `llm_call` row sets `llm_result.llm_call_id` null through the foreign key, which without
this index reads every cached result for each call deleted.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sourcing_run", sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_llm_result_llm_call_id"), "llm_result", ["llm_call_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_result_llm_call_id"), table_name="llm_result")
    op.drop_column("sourcing_run", "pruned_at")

"""Retention: a run whose detail was pruned says so, and the model cache finds its call fast.

`sourcing_run.pruned_at` marks a run whose run-scoped rows (its listings, candidates, pro-formas,
signals, narratives, brief and delivery ledger) were deleted after the retention window; the run's
own row stays, with its counts and error. `ix_llm_result_llm_call_id` closes a known gap: deleting
an old `llm_call` row sets `llm_result.llm_call_id` null through the foreign key, which without
this index reads every cached result for each call deleted.

Four more indexes serve the prune's "is anything still using this listing or candidate" checks,
which look a row up by a column that is not the first of its primary key. Measured on a scratch
database with 30,000 listings, 30,000 candidates and 36,000 rows each in `run_listing` and
`run_candidate`, the two orphan queries took 188 ms and 133 ms without them and 35 ms and 33 ms
with them; without them the cost grows with the square of the table. Every other prune predicate
is a date or a status on a table of at most tens of thousands of rows, where `EXPLAIN` shows a
sequential scan that stops after the first 1,000 matches, or reads a few milliseconds of pages once
a week. Those columns get no index: it would slow the writes to `llm_call` and `job`.

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
    # Deleting a candidate nulls llm_call.candidate_id through the foreign key (the call's cost and
    # date stay): without an index that reads the whole ledger once per candidate deleted.
    op.create_index(op.f("ix_llm_call_candidate_id"), "llm_call", ["candidate_id"])
    op.create_index(op.f("ix_run_listing_listing_id"), "run_listing", ["listing_id"])
    op.create_index(op.f("ix_run_candidate_candidate_id"), "run_candidate", ["candidate_id"])
    op.create_index(
        op.f("ix_run_candidate_primary_listing_id"), "run_candidate", ["primary_listing_id"]
    )
    op.create_index(op.f("ix_candidate_signals_listing_id"), "candidate_signals", ["listing_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_call_candidate_id"), table_name="llm_call")
    op.drop_index(op.f("ix_candidate_signals_listing_id"), table_name="candidate_signals")
    op.drop_index(op.f("ix_run_candidate_primary_listing_id"), table_name="run_candidate")
    op.drop_index(op.f("ix_run_candidate_candidate_id"), table_name="run_candidate")
    op.drop_index(op.f("ix_run_listing_listing_id"), table_name="run_listing")
    op.drop_index(op.f("ix_llm_result_llm_call_id"), table_name="llm_result")
    op.drop_column("sourcing_run", "pruned_at")

"""Verified model results: the input-hash cache, and each run's signals and narratives.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# New constraint names are wrapped in op.f(), which marks a name as final. Since 0008 the naming
# convention no longer prefixes a name that already carries the table, so it would not matter, but
# 0007 was written before that and the explicit marking is harmless.
SIGNALS_STATUSES = "'extracted', 'fields_only', 'failed', 'deferred'"
SIGNALS_REASONS = (
    "'no_remarks', 'llm_not_configured', 'budget', 'provider_error', 'structured_error', "
    "'refusal', 'replay_error'"
)
NARRATIVE_STATUSES = "'accepted', 'rejected', 'failed', 'deferred', 'not_eligible'"
NARRATIVE_REASONS = (
    "'proforma_no_arv', 'proforma_unsizable', 'figure_check', 'basis_check', 'length_check', "
    "'budget', 'llm_not_configured', 'provider_error', 'structured_error', 'refusal', "
    "'replay_error'"
)


def upgrade() -> None:
    op.create_table(
        "llm_result",
        sa.Column("prompt_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("input_sha256", sa.CHAR(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("llm_call_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "prompt_id", "prompt_version", "tier", "input_sha256", name="pk_llm_result"
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["llm_call.id"],
            name="fk_llm_result_llm_call_id_llm_call",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "prompt_id ~ '^[a-z][a-z0-9_.-]{0,99}$'", name=op.f("ck_llm_result_prompt_id")
        ),
        sa.CheckConstraint("prompt_version >= 1", name=op.f("ck_llm_result_prompt_version")),
        sa.CheckConstraint("tier IN ('small', 'mid', 'large')", name=op.f("ck_llm_result_tier")),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_llm_result_input_sha256")
        ),
    )

    op.create_table(
        "candidate_signals",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "candidate_id", name="pk_candidate_signals"),
        sa.ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            ["run_candidate.run_id", "run_candidate.candidate_id"],
            name="fk_candidate_signals_run_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"], ["listing.id"], name="fk_candidate_signals_listing_id_listing"
        ),
        sa.CheckConstraint(
            f"status IN ({SIGNALS_STATUSES})", name=op.f("ck_candidate_signals_status")
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR reason IN ({SIGNALS_REASONS})",
            name=op.f("ck_candidate_signals_reason_known"),
        ),
        sa.CheckConstraint(
            "(status = 'extracted') = (reason IS NULL)", name=op.f("ck_candidate_signals_reason")
        ),
    )

    op.create_table(
        "candidate_narrative",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("input_sha256", sa.CHAR(64)),
        sa.Column("result", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "candidate_id", name="pk_candidate_narrative"),
        sa.ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            ["run_candidate.run_id", "run_candidate.candidate_id"],
            name="fk_candidate_narrative_run_candidate",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"status IN ({NARRATIVE_STATUSES})", name=op.f("ck_candidate_narrative_status")
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR reason IN ({NARRATIVE_REASONS})",
            name=op.f("ck_candidate_narrative_reason_known"),
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (reason IS NULL)", name=op.f("ck_candidate_narrative_reason")
        ),
        sa.CheckConstraint(
            "input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_candidate_narrative_input_sha256"),
        ),
        sa.CheckConstraint(
            "status <> 'not_eligible' OR input_sha256 IS NULL",
            name=op.f("ck_candidate_narrative_not_eligible_no_digest"),
        ),
    )
    op.create_index(
        "ix_candidate_narrative_run_id_status", "candidate_narrative", ["run_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_narrative_run_id_status", table_name="candidate_narrative")
    op.drop_table("candidate_narrative")
    op.drop_table("candidate_signals")
    op.drop_table("llm_result")

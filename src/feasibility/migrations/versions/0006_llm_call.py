"""The model call ledger: one row per call, in every outcome.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USD = sa.Numeric(12, 6)


def upgrade() -> None:
    op.create_table(
        "llm_call",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.BigInteger()),
        sa.Column("candidate_id", sa.BigInteger()),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("prompt_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("input_sha256", sa.CHAR(64), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("billable", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cache_creation_input_tokens", sa.Integer()),
        sa.Column("cache_read_input_tokens", sa.Integer()),
        sa.Column("cost_usd", USD),
        sa.Column("reserved_usd", USD, nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column(
            "called_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_call"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sourcing_run.id"],
            name="fk_llm_call_run_id_sourcing_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidate.id"],
            name="fk_llm_call_candidate_id_candidate",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "stage IN ('signals', 'narrative', 'eval')", name=op.f("ck_llm_call_stage")
        ),
        sa.CheckConstraint(
            "prompt_id ~ '^[a-z][a-z0-9_.-]{0,99}$'", name=op.f("ck_llm_call_prompt_id")
        ),
        sa.CheckConstraint("prompt_version >= 1", name=op.f("ck_llm_call_prompt_version")),
        sa.CheckConstraint("attempts >= 1", name=op.f("ck_llm_call_attempts")),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_llm_call_input_sha256")
        ),
        sa.CheckConstraint("tier IN ('small', 'mid', 'large')", name=op.f("ck_llm_call_tier")),
        sa.CheckConstraint("mode IN ('replay', 'record', 'live')", name=op.f("ck_llm_call_mode")),
        sa.CheckConstraint(
            "billable = (mode IN ('record', 'live'))", name=op.f("ck_llm_call_billable")
        ),
        sa.CheckConstraint(
            "outcome IN ('ok', 'structured_error', 'refusal', 'provider_error', "
            "'budget_refused', 'replay_error')",
            name=op.f("ck_llm_call_outcome"),
        ),
        sa.CheckConstraint(
            "(cost_usd IS NULL OR cost_usd >= 0) AND reserved_usd >= 0 "
            "AND (input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0) "
            "AND (cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0) "
            "AND (latency_ms IS NULL OR latency_ms >= 0)",
            name=op.f("ck_llm_call_nonnegative"),
        ),
        sa.CheckConstraint(
            "(outcome = 'ok') = (cost_usd IS NOT NULL)", name=op.f("ck_llm_call_ok_has_cost")
        ),
    )
    op.create_index("ix_llm_call_run_id", "llm_call", ["run_id"])
    op.create_index(
        "ix_llm_call_billable_called_at",
        "llm_call",
        ["called_at"],
        postgresql_where=sa.text("billable"),
    )


def downgrade() -> None:
    op.drop_index("ix_llm_call_billable_called_at", table_name="llm_call")
    op.drop_index("ix_llm_call_run_id", table_name="llm_call")
    op.drop_table("llm_call")

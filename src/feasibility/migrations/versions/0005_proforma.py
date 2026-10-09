"""The pro-forma of each ranked candidate, per run.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 2)
# The engine accepts comps of up to 15 digits and a tiny ARV makes a margin enormous, so the
# figures it computes must fit wider columns than a listing price does.
RESULT_MONEY = sa.Numeric(20, 2)
RESULT_RATIO = sa.Numeric(20, 4)


def upgrade() -> None:
    op.create_table(
        "proforma",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("estimate_fetched_on", sa.Date()),
        sa.Column("offer_price", MONEY, nullable=False),
        sa.Column("arv", RESULT_MONEY),
        sa.Column("total_cost", RESULT_MONEY),
        sa.Column("profit", RESULT_MONEY),
        sa.Column("margin", RESULT_RATIO),
        sa.Column("roi", RESULT_RATIO),
        sa.Column("annualized_return", RESULT_RATIO),
        sa.Column("max_offer", RESULT_MONEY),
        sa.Column("flags", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("result", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "candidate_id", name="pk_proforma"),
        sa.ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            ["run_candidate.run_id", "run_candidate.candidate_id"],
            name="fk_proforma_run_candidate",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('computed', 'no_arv', 'unsizable')", name="ck_proforma_status"
        ),
        # roi and annualized_return are NULL when no cash is invested, so they are not required.
        sa.CheckConstraint(
            "(status = 'computed') = (arv IS NOT NULL AND total_cost IS NOT NULL "
            "AND profit IS NOT NULL AND margin IS NOT NULL)",
            name="ck_proforma_computed_has_outputs",
        ),
        sa.CheckConstraint(
            "(status = 'computed') = (reason IS NULL)",
            name="ck_proforma_reason_iff_not_computed",
        ),
    )
    op.create_index("ix_proforma_run_id_status", "proforma", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_proforma_run_id_status", table_name="proforma")
    op.drop_table("proforma")

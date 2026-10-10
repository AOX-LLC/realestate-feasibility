"""The brief: what is delivered for a run, built by code and stored for audit.

Also two columns on the run: the data mode it ran in (a brief says "synthetic" from the run, not
from whoever reads it) and when its last stage ended (a brief is built only after that).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sourcing_run",
        sa.Column("data_mode", sa.Text(), server_default="mock", nullable=False),
    )
    op.add_column(
        "sourcing_run", sa.Column("stages_finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_sourcing_run_data_mode"), "sourcing_run", "data_mode IN ('mock', 'live')"
    )
    # Runs that exist now finished all their stages (or recorded the error that stopped them).
    op.execute(
        "UPDATE sourcing_run SET stages_finished_at = finished_at WHERE status = 'completed'"
    )
    op.create_table(
        "brief",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.SmallInteger(), nullable=False),
        sa.Column("completeness", sa.Text(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column(
            "built_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_brief")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sourcing_run.id"],
            name=op.f("fk_brief_run_id_sourcing_run"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("version = 1", name=op.f("ck_brief_version")),
        sa.CheckConstraint(
            "completeness IN ('complete', 'partial')", name=op.f("ck_brief_completeness")
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_brief_content_sha256")
        ),
    )


def downgrade() -> None:
    op.drop_table("brief")
    # Dropping the column drops its check constraint, whatever the constraint is called.
    op.drop_column("sourcing_run", "stages_finished_at")
    op.drop_column("sourcing_run", "data_mode")

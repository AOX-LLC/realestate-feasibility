"""The brief: what is delivered for a run, built by code and stored for audit.

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

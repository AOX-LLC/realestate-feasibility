"""Schema polish: check constraints named once, not twice, and an index on the GIS parcel id.

The first five migrations spelled each check constraint's whole name (`ck_proforma_status`) and
the naming convention then added `ck_<table>_` in front of it, so the database holds
`ck_proforma_ck_proforma_status`. The convention no longer prefixes a name that already carries the
prefix (see `tables._check_name`), so a database built from scratch has the single name from the
start; this revision renames the doubled ones in a database that was built before.

It also indexes `parcel (market, gis_parcel_id)`: the pro-forma stage reads the parcels of a GIS
group by that id, together with the zip, and until now only the (market, zip5) index could serve it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
DOUBLED_CHECKS = sa.text(
    """
    SELECT c.conrelid::regclass::text AS table_name, c.conname AS name
    FROM pg_constraint c
    WHERE c.contype = 'c'
      AND c.connamespace = current_schema()::regnamespace
      AND c.conname ~ '^ck_(.+)_ck_\\1_'
    ORDER BY 1, 2
    """
)


def _single_name(table: str, name: str) -> str:
    return name[len(f"ck_{table}_") :]


def upgrade() -> None:
    for table, name in op.get_bind().execute(DOUBLED_CHECKS).all():
        if not name.startswith(f"ck_{table}_ck_{table}_"):
            continue  # a name that merely repeats a word, not a doubled prefix
        single = _single_name(table, name)
        if not (IDENTIFIER.match(table) and IDENTIFIER.match(name) and IDENTIFIER.match(single)):
            raise ValueError(f"unexpected constraint name {name!r} on {table!r}")
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" TO "{single}"')
    op.create_index("ix_parcel_market_gis_parcel_id", "parcel", ["market", "gis_parcel_id"])


def downgrade() -> None:
    """Drops the index. The names are not put back: both spellings were the same constraint, and
    the earlier revisions create the single name on a database built from scratch."""
    op.drop_index("ix_parcel_market_gis_parcel_id", table_name="parcel")

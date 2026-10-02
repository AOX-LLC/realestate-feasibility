"""Alembic environment. The database URL comes from Settings, never from an ini file."""

from alembic import context

from feasibility.db import get_engine
from feasibility.tables import metadata

config = context.config


def run_migrations() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    with get_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()

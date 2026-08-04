from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model persisted through a `Store`.

    Applications and connectors define their own models against this `Base` so that a
    single `Store.migrate()` (and one Alembic revision history) covers all of them.
    """

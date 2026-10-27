"""
Base declarative configuration for SQLAlchemy ORM models.

CRITICAL INVARIANT:
The Microsoft SQL Server database schema and its tables are pre-existing.
NEVER call Base.metadata.create_all() or Base.metadata.drop_all().
ORM models defined using this Base must adapt to the existing database schema.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Shared DeclarativeBase for all application ORM entities.
    No automatic DDL execution is permitted.
    """
    pass

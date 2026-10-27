"""
Centralized SQLAlchemy session management and FastAPI dependency injection.

Manages Session lifecycle across HTTP requests and background services,
reusing the existing database connection pool from app.database.connection.
"""

from typing import Generator, ContextManager
from contextlib import contextmanager
from sqlalchemy.orm import sessionmaker, Session
from app.database.connection import get_engine
from app.core.logging import logger

_session_factory = None


def get_session_factory() -> sessionmaker:
    """
    Returns the singleton sessionmaker instance bound to the shared database engine.
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = sessionmaker(
            bind=engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False
        )
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a transactional database Session per request.
    Ensures sessions are cleanly closed when the request lifecycle ends.
    """
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
    except Exception as exc:
        session.rollback()
        logger.error(f"[DATABASE_SESSION] Exception during request session, rolled back: {exc}")
        raise
    finally:
        session.close()


@contextmanager
def get_session_context() -> Generator[Session, None, None]:
    """
    Context manager providing a scoped Session for background workers,
    tasks, or service-managed transactions.
    """
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
    except Exception as exc:
        session.rollback()
        logger.error(f"[DATABASE_SESSION] Exception during context session, rolled back: {exc}")
        raise
    finally:
        session.close()

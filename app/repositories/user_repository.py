from typing import Optional, List, Dict, Any, Callable
from sqlalchemy import text, Engine
from sqlalchemy.orm import Session
from app.database.connection import get_engine
from app.core.logging import logger


class DuplicateUserError(Exception):
    """Raised when more than one active user record matches the username."""
    pass


class UserRepository:
    """
    Repository layer centralizing all database operations for the Users entity.
    Isolates dialect-specific SQL, connection handling, and data mapping.
    """

    def __init__(
        self,
        engine_provider: Optional[Callable[[], Engine]] = None,
        session: Optional[Session] = None
    ):
        self._engine_provider = engine_provider or get_engine
        self._session = session

    def _get_engine(self) -> Engine:
        return self._engine_provider()

    def find_users_by_username(self, username: str) -> List[Dict[str, Any]]:
        """
        Executes a dialect-aware query to find matching user records by username (case-insensitive, trimmed).
        Limits results to 2 to efficiently detect duplicates without scanning the whole table.
        """
        normalized_username = username.strip()
        if not normalized_username:
            return []

        if self._session is not None:
            bind = self._session.get_bind()
            dialect_name = getattr(bind, "name", "sqlite").lower()
        else:
            engine = self._get_engine()
            dialect_name = getattr(engine, "name", "mssql").lower()

        if dialect_name == "mssql":
            query = text(
                "SELECT TOP 2 [UserName], [Password] "
                "FROM [dbo].[Users] "
                "WHERE LOWER(LTRIM(RTRIM([UserName]))) = LOWER(:username)"
            )
        elif dialect_name in ("postgresql", "postgres"):
            query = text(
                'SELECT "UserName", "Password" '
                'FROM public."Users" '
                'WHERE LOWER(TRIM("UserName")) = LOWER(:username) '
                'LIMIT 2'
            )
        elif dialect_name in ("mysql", "mariadb"):
            query = text(
                "SELECT `UserName`, `Password` "
                "FROM `Users` "
                "WHERE LOWER(TRIM(`UserName`)) = LOWER(:username) "
                "LIMIT 2"
            )
        else:
            # SQLite / default fallback
            query = text(
                'SELECT "UserName", "Password" '
                'FROM "Users" '
                'WHERE LOWER(TRIM("UserName")) = LOWER(:username) '
                'LIMIT 2'
            )

        if self._session is not None:
            rows = self._session.execute(query, {"username": normalized_username}).mappings().all()
            return [dict(row) for row in rows]

        with engine.connect() as connection:
            rows = connection.execute(query, {"username": normalized_username}).mappings().all()
            return [dict(row) for row in rows]

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Returns exactly one user record if found.
        Raises DuplicateUserError if multiple rows match the given username.
        Returns None if no matching user exists.
        """
        rows = self.find_users_by_username(username)
        if not rows:
            return None
        if len(rows) > 1:
            raise DuplicateUserError(f"Duplicate user records found for username '{username}'")
        return rows[0]


user_repository = UserRepository()

import base64
import hashlib
import hmac
import re

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.logging import logger
from app.database.connection import get_engine

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    authenticated: bool
    username: str


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify the legacy SHA-1 and Django PBKDF2 formats found in dbo.Users."""
    if not stored_hash:
        return False

    if re.fullmatch(r"[0-9a-fA-F]{40}", stored_hash):
        candidate = hashlib.sha1(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate.lower(), stored_hash.lower())

    parts = stored_hash.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
            if iterations <= 0 or iterations > 10_000_000:
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                parts[2].encode("utf-8"),
                iterations
            )
            candidate = base64.b64encode(digest).decode("ascii").strip()
            return hmac.compare_digest(candidate, parts[3])
        except (ValueError, UnicodeError, OverflowError, TypeError):
            return False

    return False


def _fetch_user_rows(connection, username: str, dialect_name: str):
    """
    Executes a dialect-safe query to find up to 2 user rows matching the username.
    Supports MSSQL, PostgreSQL, MySQL, and SQLite.
    """
    dialect = dialect_name.lower()

    if dialect == "mssql":
        query = text(
            "SELECT TOP 2 [UserName], [Password] "
            "FROM [dbo].[Users] "
            "WHERE LOWER(LTRIM(RTRIM([UserName]))) = LOWER(:username)"
        )
        return connection.execute(query, {"username": username}).mappings().all()

    # For PostgreSQL, MySQL, SQLite:
    # 1. Try to find the exact table and column names via inspection if available
    table_name = "Users"
    user_col = "UserName"
    pass_col = "Password"
    schema = None

    try:
        from sqlalchemy import inspect
        inspector = inspect(connection)

        # Check default schema tables first
        tables = inspector.get_table_names()
        matched = next((t for t in tables if t.lower() == "users"), None)

        # If not found in default schema, search all schemas (e.g., 'public', 'dbo')
        if not matched:
            for s in inspector.get_schema_names():
                try:
                    s_tables = inspector.get_table_names(schema=s)
                    m = next((t for t in s_tables if t.lower() == "users"), None)
                    if m:
                        matched = m
                        schema = s
                        break
                except Exception:
                    continue

        if matched:
            table_name = matched
            cols = [c["name"] for c in inspector.get_columns(matched, schema=schema)]
            u = next((c for c in cols if c.lower() in ("username", "user_name")), None)
            p = next((c for c in cols if c.lower() in ("password", "password_hash", "pass_hash", "pwd")), None)
            if u:
                user_col = u
            if p:
                pass_col = p
    except Exception as inspect_err:
        logger.debug(f"[AUTH] Inspector discovery skipped or failed: {inspect_err}")

    # Build dialect-specific query
    if dialect in ("postgresql", "postgres"):
        if schema:
            tbl_sql = f'"{schema}"."{table_name}"'
        else:
            tbl_sql = f'"{table_name}"'
        user_sql = f'"{user_col}"'
        pass_sql = f'"{pass_col}"'
        query = text(
            f"SELECT {user_sql}, {pass_sql} "
            f"FROM {tbl_sql} "
            f"WHERE LOWER(TRIM({user_sql})) = LOWER(:username) "
            f"LIMIT 2"
        )
        try:
            return connection.execute(query, {"username": username}).mappings().all()
        except Exception:
            # Fallback to unquoted lowercase if double-quoted query failed
            fallback_query = text(
                "SELECT username, password FROM users "
                "WHERE LOWER(TRIM(username)) = LOWER(:username) "
                "LIMIT 2"
            )
            return connection.execute(fallback_query, {"username": username}).mappings().all()

    elif dialect in ("mysql", "mariadb"):
        tbl_sql = f"`{table_name}`"
        user_sql = f"`{user_col}`"
        pass_sql = f"`{pass_col}`"
        query = text(
            f"SELECT {user_sql}, {pass_sql} "
            f"FROM {tbl_sql} "
            f"WHERE LOWER(TRIM({user_sql})) = LOWER(:username) "
            f"LIMIT 2"
        )
        return connection.execute(query, {"username": username}).mappings().all()

    else:
        # SQLite and others
        tbl_sql = f'"{table_name}"'
        user_sql = f'"{user_col}"'
        pass_sql = f'"{pass_col}"'
        query = text(
            f"SELECT {user_sql}, {pass_sql} "
            f"FROM {tbl_sql} "
            f"WHERE LOWER(TRIM({user_sql})) = LOWER(:username) "
            f"LIMIT 2"
        )
        return connection.execute(query, {"username": username}).mappings().all()


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    engine = get_engine()
    dialect_name = getattr(engine, "name", "mssql")

    try:
        with engine.connect() as connection:
            rows = _fetch_user_rows(connection, username, dialect_name)
    except Exception:
        logger.exception("[AUTH] User lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login service is temporarily unavailable"
        )

    if len(rows) != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    first_row = dict(rows[0])
    row_lower = {str(k).lower(): v for k, v in first_row.items()}
    stored_password = str(row_lower.get("password") or row_lower.get("password_hash") or "")

    if not verify_password(payload.password, stored_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    matched_username = str(row_lower.get("username") or row_lower.get("user_name") or first_row.get("UserName") or username).strip()
    return LoginResponse(authenticated=True, username=matched_username)
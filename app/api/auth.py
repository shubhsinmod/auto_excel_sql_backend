
import base64
import hashlib
import hmac
import re

try:
    import bcrypt
except ImportError:
    bcrypt = None

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
    """Verify bcrypt, legacy SHA-1, and Django PBKDF2-SHA256 hashes."""
    if not stored_hash:
        return False

    # Supabase bcrypt hashes: $2a$, $2b$, or $2y$
    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        if bcrypt is None:
            logger.error("[AUTH] bcrypt module is not installed to verify bcrypt password hash")
            return False
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                stored_hash.encode("utf-8")
            )
        except (ValueError, TypeError):
            return False

    # Legacy SHA-1
    if re.fullmatch(r"[0-9a-fA-F]{40}", stored_hash):
        candidate = hashlib.sha1(
            password.encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(
            candidate.lower(), stored_hash.lower()
        )

    # Django PBKDF2-SHA256
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
            candidate = base64.b64encode(digest).decode("ascii")
            return hmac.compare_digest(candidate, parts[3])
        except (ValueError, UnicodeError, OverflowError, TypeError):
            return False

    return False


def _fetch_user_rows(connection, username: str, dialect_name: str):
    dialect = dialect_name.lower()

    if dialect == "mssql":
        query = text(
            "SELECT TOP 2 [UserName], [Password] "
            "FROM [dbo].[Users] "
            "WHERE LOWER(LTRIM(RTRIM([UserName]))) = LOWER(:username)"
        )
        return connection.execute(
            query, {"username": username}
        ).mappings().all()

    if dialect in ("postgresql", "postgres"):
        query = text(
            'SELECT "UserName", "Password" '
            'FROM public."Users" '
            'WHERE LOWER(TRIM("UserName")) = LOWER(:username) '
            'LIMIT 2'
        )
        return connection.execute(
            query, {"username": username}
        ).mappings().all()

    if dialect in ("mysql", "mariadb"):
        query = text(
            "SELECT `UserName`, `Password` "
            "FROM `Users` "
            "WHERE LOWER(TRIM(`UserName`)) = LOWER(:username) "
            "LIMIT 2"
        )
        return connection.execute(
            query, {"username": username}
        ).mappings().all()

    # SQLite
    query = text(
        'SELECT "UserName", "Password" '
        'FROM "Users" '
        'WHERE LOWER(TRIM("UserName")) = LOWER(:username) '
        'LIMIT 2'
    )
    return connection.execute(
        query, {"username": username}
    ).mappings().all()


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    username = payload.username.strip()

    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    engine = get_engine()
    dialect_name = getattr(engine, "name", "mssql")

    try:
        with engine.connect() as connection:
            rows = _fetch_user_rows(
                connection, username, dialect_name
            )
    except Exception:
        logger.exception("[AUTH] User lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login service is temporarily unavailable"
        )

    if len(rows) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    row = dict(rows[0])
    row_lower = {str(k).lower(): v for k, v in row.items()}

    stored_password = str(
        row_lower.get("password")
        or row_lower.get("password_hash")
        or ""
    )

    if not verify_password(payload.password, stored_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    matched_username = str(
        row_lower.get("username") or username
    ).strip()

    return LoginResponse(
        authenticated=True,
        username=matched_username
    )

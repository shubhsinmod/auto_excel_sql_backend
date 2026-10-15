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


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    query = text(
        "SELECT TOP 2 [UserName], [Password] "
        "FROM [dbo].[Users] "
        "WHERE LOWER(LTRIM(RTRIM([UserName]))) = LOWER(:username)"
    )

    try:
        with get_engine().connect() as connection:
            rows = connection.execute(query, {"username": username}).mappings().all()
    except Exception:
        logger.exception("[AUTH] User lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login service is temporarily unavailable"
        )

    if len(rows) != 1 or not verify_password(payload.password, str(rows[0].get("Password") or "")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    return LoginResponse(authenticated=True, username=str(rows[0]["UserName"]).strip())
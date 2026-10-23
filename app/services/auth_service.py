import base64
import hashlib
import hmac
import re
from typing import Optional, Dict, Any

try:
    import bcrypt
except ImportError:
    bcrypt = None

from app.repositories.user_repository import UserRepository, user_repository, DuplicateUserError
from app.models.user import LoginResponse
from app.core.logging import logger


class AuthenticationError(Exception):
    """Raised when authentication fails due to invalid credentials."""
    pass


class AuthUnavailableError(Exception):
    """Raised when authentication service cannot reach the user store."""
    pass


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Verifies passwords against bcrypt, legacy SHA-1, and Django PBKDF2-SHA256 hashes.
    Sensitive credentials are never logged or exposed.
    """
    if not stored_hash:
        return False

    # 1. Supabase / standard bcrypt hashes: $2a$, $2b$, or $2y$
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

    # 2. Legacy SHA-1 (hex digest of length 40)
    if re.fullmatch(r"[0-9a-fA-F]{40}", stored_hash):
        candidate = hashlib.sha1(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate.lower(), stored_hash.lower())

    # 3. Django PBKDF2-SHA256 (format: pbkdf2_sha256$iterations$salt$hash)
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


class AuthService:
    """
    Service layer handling authentication workflows, password verification,
    and user credential validation.
    """

    def __init__(self, user_repo: Optional[UserRepository] = None):
        self._user_repo = user_repo or user_repository

    def authenticate_user(self, username: str, password: str) -> LoginResponse:
        """
        Validates username and password against the Users repository.
        Returns LoginResponse on success; raises AuthenticationError on invalid credentials;
        raises AuthUnavailableError if the database is unreachable.
        """
        normalized_username = (username or "").strip()
        if not normalized_username:
            raise AuthenticationError("Invalid username or password")

        try:
            user_rows = self._user_repo.find_users_by_username(normalized_username)
        except Exception as e:
            logger.exception(f"[AUTH_SERVICE] User lookup failed for username '{normalized_username}': {e}")
            raise AuthUnavailableError("Login service is temporarily unavailable")

        # Must match exactly one user record; multiple matches are rejected as ambiguous
        if len(user_rows) != 1:
            raise AuthenticationError("Invalid username or password")

        row = dict(user_rows[0])
        row_lower = {str(k).lower(): v for k, v in row.items()}

        stored_password = str(
            row_lower.get("password")
            or row_lower.get("password_hash")
            or ""
        )

        if not verify_password(password, stored_password):
            raise AuthenticationError("Invalid username or password")

        matched_username = str(
            row_lower.get("username") or normalized_username
        ).strip()

        return LoginResponse(
            authenticated=True,
            username=matched_username
        )


auth_service = AuthService()

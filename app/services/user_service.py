from typing import Optional, Dict, Any
from app.repositories.user_repository import UserRepository, user_repository
from app.models.user import UserRecord
from app.core.logging import logger


class UserService:
    """
    Service layer handling user management and query workflows.
    Decoupled from authentication/token issuance.
    """

    def __init__(self, user_repo: Optional[UserRepository] = None):
        self._user_repo = user_repo or user_repository

    def get_user_profile(self, username: str) -> Optional[UserRecord]:
        """
        Retrieves user information without exposing password hashes.
        """
        normalized_username = (username or "").strip()
        if not normalized_username:
            return None

        user_row = self._user_repo.get_user_by_username(normalized_username)
        if not user_row:
            return None

        row_lower = {str(k).lower(): v for k, v in user_row.items()}
        return UserRecord(
            username=str(row_lower.get("username") or normalized_username).strip(),
            password_hash=str(row_lower.get("password") or ""),
            is_active=True
        )

    def user_exists(self, username: str) -> bool:
        """
        Checks whether a user exists in the database.
        """
        normalized_username = (username or "").strip()
        if not normalized_username:
            return False
        return bool(self._user_repo.find_users_by_username(normalized_username))


user_service = UserService()

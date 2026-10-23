from fastapi import APIRouter, HTTPException, status

from app.models.user import LoginRequest, LoginResponse
from app.services.auth_service import (
    auth_service,
    AuthService,
    AuthenticationError,
    AuthUnavailableError,
    verify_password
)
from app.repositories.user_repository import UserRepository, user_repository
from app.database.connection import get_engine as default_get_engine
from app.core.logging import logger

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

# Module-level engine accessor preserved for test monkeypatching and backward compatibility
get_engine = default_get_engine


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    """
    Authenticate user credentials against the persistent user repository.
    Controller validates payload and delegates authentication business logic to AuthService.
    """
    username = payload.username.strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    # Use the current module-level get_engine so test monkeypatching is preserved
    scoped_user_repo = UserRepository(engine_provider=lambda: get_engine())
    scoped_auth_service = AuthService(user_repo=scoped_user_repo)

    try:
        return scoped_auth_service.authenticate_user(username=username, password=payload.password)
    except AuthenticationError as auth_err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(auth_err)
        )
    except AuthUnavailableError as unavail_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(unavail_err)
        )
    except Exception as e:
        logger.exception(f"[AUTH_CONTROLLER] Unexpected error during login: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login service is temporarily unavailable"
        )

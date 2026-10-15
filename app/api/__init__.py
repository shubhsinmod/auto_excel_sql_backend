"""
API routes package.
"""
from app.api.database import router as database_router
from app.api.excel import router as excel_router
from app.api.migration import router as migration_router
from app.api.synonyms import router as synonyms_router
from app.api.auth import router as auth_router

__all__ = [
    "database_router",
    "excel_router",
    "migration_router",
    "synonyms_router",
    "auth_router"
]

from typing import Dict, Any, Optional
from app.database.connection import test_db_connection, get_engine, db_manager
from app.database.metadata import extract_database_metadata, clear_schema_cache
from app.models.schema import DatabaseSchemaMetadata, ConnectionTestResponse
from app.core.config import settings
from app.core.logging import logger


class DatabaseService:
    """
    Service layer for database operations.
    """

    @staticmethod
    def test_connection(request=None) -> ConnectionTestResponse:
        """Test either the configured connection or an explicitly supplied connection."""
        if request is not None:
            data = db_manager.test_connection(
                server=getattr(request, "server", None) or getattr(request, "db_host", None) or settings.DB_HOST,
                database=getattr(request, "database", None) or getattr(request, "db_name", None) or settings.DB_NAME,
                username=getattr(request, "username", None) or getattr(request, "db_user", None) or settings.DB_USER or "",
                password=getattr(request, "password", None) or getattr(request, "db_password", None) or settings.DB_PASSWORD or "",
                port=getattr(request, "port", None) or getattr(request, "db_port", None) or settings.DB_PORT,
                driver=getattr(request, "driver", None) or settings.DB_DRIVER,
                trusted_connection=(getattr(request, "trusted_connection", None)
                                    if getattr(request, "trusted_connection", None) is not None
                                    else settings.DB_TRUSTED_CONNECTION),
            )
            return ConnectionTestResponse(
                connected=data["connected"], database=data.get("database", ""),
                db_type="mssql", server=data.get("server"), version=data.get("version"),
                message=data.get("message", ""), error=data.get("error")
            )

        success, message = test_db_connection()
        return ConnectionTestResponse(
            connected=success, database=settings.DB_NAME, db_type=settings.DB_TYPE,
            server=settings.DB_HOST, message=message, error=None if success else message
        )

    @staticmethod
    def get_schema_metadata(use_cache: bool = True) -> DatabaseSchemaMetadata:
        """
        Extracts complete database schema metadata including tables, columns, PK, FK, and identity info.
        """
        engine = get_engine()
        return extract_database_metadata(engine, use_cache=use_cache)

    @staticmethod
    def refresh_schema_metadata() -> DatabaseSchemaMetadata:
        """
        Forces cache invalidation and re-introspects the database schema.
        """
        clear_schema_cache()
        return DatabaseService.get_schema_metadata(use_cache=False)


database_service = DatabaseService()

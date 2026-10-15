import time
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional

from app.models.schema import ConnectionTestResponse, DatabaseSchemaMetadata
from app.services.database_service import database_service
from app.core.config import settings
from app.core.logging import logger
from app.database.connection import test_db_connection

router = APIRouter(prefix="/api/database", tags=["Database"])


class ConnectRequest(BaseModel):
    # Accept both current frontend names and the older server/database names.
    db_type: Optional[str] = None
    db_host: Optional[str] = Field(default=None, validation_alias="db_host")
    db_port: Optional[int] = None
    db_name: Optional[str] = Field(default=None, validation_alias="db_name")
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    server: Optional[str] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    driver: Optional[str] = None
    trusted_connection: Optional[bool] = None


@router.get("/test", response_model=ConnectionTestResponse)
def test_connection():
    """
    Test database connection using configured environment variables.
    """
    return database_service.test_connection()


@router.post("/connect", response_model=ConnectionTestResponse)
def connect_database(payload: ConnectRequest):
    """
    Dynamically update database connection parameters and test connectivity.
    """
    return database_service.test_connection(payload)


@router.get("/schema", response_model=DatabaseSchemaMetadata)
def get_schema():
    """
    Automatically introspect and extract the complete database schema with API timing instrumentation.
    """
    t0 = time.time()
    logger.info("[API] STEP3 HANDLER START | GET /api/database/schema")
    try:
        result = database_service.get_schema_metadata()
        t1 = time.time()
        logger.info(f"[API] STEP3 HANDLER END | {t1 - t0:.3f}s")
        return result
    except Exception as e:
        logger.error(f"[API] Error extracting database schema: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to introspect database schema: {str(e)}"
        )

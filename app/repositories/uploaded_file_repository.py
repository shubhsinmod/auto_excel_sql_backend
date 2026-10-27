"""
UploadedFileRepository implementation using SQLAlchemy ORM.

Centralizes database operations for dbo.UploadedFiles.
Leverages SQLAlchemy ORM session lifecycle and identity generation.
"""

import datetime
from typing import Optional, List, Dict, Any, Callable
from sqlalchemy import select, func, Engine
from sqlalchemy.orm import Session
from app.models.uploaded_file import UploadedFile
from app.database.session import get_session_context, get_session_factory
from app.database.connection import get_engine
from app.core.logging import logger


class UploadedFileRepository:
    """
    Repository layer centralizing all database operations for dbo.UploadedFiles.
    Executes ORM operations using injected Sessions or thread-safe session contexts.
    """

    def __init__(
        self,
        session: Optional[Session] = None,
        engine_provider: Optional[Callable[[], Any]] = None
    ):
        self._session = session
        self._engine_provider = engine_provider

    def create_file_record(self, file_name: str) -> int:
        """
        Creates a new file record in dbo.UploadedFiles with status 'Processing'
        and returns the generated FileID.
        """
        clean_name = str(file_name or "uploaded_file.xlsx")[:255]

        if self._session is not None:
            record = UploadedFile(FileName=clean_name, Status="Processing")
            self._session.add(record)
            self._session.flush()
            file_id = int(record.FileID)
            logger.info(f"[UPLOADED_FILE_REPO] Registered uploaded file '{clean_name}' with FileID={file_id}")
            return file_id

        with get_session_context() as session:
            record = UploadedFile(FileName=clean_name, Status="Processing")
            session.add(record)
            session.commit()
            file_id = int(record.FileID)
            logger.info(f"[UPLOADED_FILE_REPO] Registered uploaded file '{clean_name}' with FileID={file_id}")
            return file_id

    def get_file_by_id(self, file_id: int) -> Optional[Dict[str, Any]]:
        """
        Retrieves a single file record by FileID.
        """
        if self._session is not None:
            record = self._session.get(UploadedFile, file_id)
            return record.to_dict() if record else None

        with get_session_context() as session:
            record = session.get(UploadedFile, file_id)
            return record.to_dict() if record else None

    def update_file_status(self, file_id: int, status: str) -> None:
        """
        Updates the Status column for a specific FileID.
        """
        if self._session is not None:
            record = self._session.get(UploadedFile, file_id)
            if record:
                record.Status = status
                self._session.flush()
                logger.info(f"[UPLOADED_FILE_REPO] Updated FileID={file_id} status to '{status}'")
            return

        with get_session_context() as session:
            record = session.get(UploadedFile, file_id)
            if record:
                record.Status = status
                session.commit()
                logger.info(f"[UPLOADED_FILE_REPO] Updated FileID={file_id} status to '{status}'")

    def file_exists(self, file_id: int) -> bool:
        """
        Checks whether a FileID exists in dbo.UploadedFiles.
        """
        try:
            if self._session is not None:
                stmt = select(func.count(UploadedFile.FileID)).where(UploadedFile.FileID == file_id)
                cnt = self._session.scalar(stmt) or 0
                return cnt > 0

            with get_session_context() as session:
                stmt = select(func.count(UploadedFile.FileID)).where(UploadedFile.FileID == file_id)
                cnt = session.scalar(stmt) or 0
                return cnt > 0
        except Exception:
            return False

    def get_total_count(self) -> int:
        """
        Returns total count of uploaded files using fast index count.
        """
        try:
            if self._session is not None:
                stmt = select(func.count(UploadedFile.FileID))
                return int(self._session.scalar(stmt) or 0)

            with get_session_context() as session:
                stmt = select(func.count(UploadedFile.FileID))
                return int(session.scalar(stmt) or 0)
        except Exception as e:
            logger.warning(f"[UPLOADED_FILE_REPO] Count query warning: {e}")
            return 0

    def list_files(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves recent uploaded files from UploadedFiles ordered by FileID DESC.
        Uses ORM select with limit.
        """
        stmt = (
            select(UploadedFile)
            .order_by(UploadedFile.FileID.desc())
            .limit(limit)
        )

        if self._session is not None:
            records = self._session.scalars(stmt).all()
            return [r.to_dict() for r in records]

        with get_session_context() as session:
            records = session.scalars(stmt).all()
            return [r.to_dict() for r in records]


uploaded_file_repository = UploadedFileRepository()

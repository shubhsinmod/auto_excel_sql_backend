from typing import List, Dict, Any, Optional
from sqlalchemy import text
from app.repositories.file_tracking_repository import file_tracking_repository, FileTrackingRepository
from app.services.history_service import history_service, HistoryService
from app.database.connection import get_engine
from app.core.logging import logger

SCHEMA_MIGRATION_SQL = """
-- 1. Create UploadedFiles table if it does not exist
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES 
    WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'UploadedFiles'
)
BEGIN
    CREATE TABLE dbo.UploadedFiles (
        FileID INT IDENTITY(1,1) PRIMARY KEY,
        FileName NVARCHAR(255) NOT NULL,
        UploadedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        Status NVARCHAR(30) NOT NULL DEFAULT 'Processing'
    );
END;

-- 2. Ensure FileID column exists on target destination tables
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Bills')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Bills' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Bills ADD FileID INT NULL;
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Order')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Order' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.[Order] ADD FileID INT NULL;
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'OrderLineItems')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'OrderLineItems' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.OrderLineItems ADD FileID INT NULL;
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Customer')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Customer' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Customer ADD FileID INT NULL;
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlements')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlements' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Settlements ADD FileID INT NULL;
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlement')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlement' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Settlement ADD FileID INT NULL;
END;

-- 3. Add Foreign Key constraints referencing dbo.UploadedFiles(FileID)
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Bills')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Bills_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Bills ADD CONSTRAINT FK_Bills_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Order')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Order_UploadedFiles')
BEGIN
    ALTER TABLE dbo.[Order] ADD CONSTRAINT FK_Order_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'OrderLineItems')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_OrderLineItems_UploadedFiles')
BEGIN
    ALTER TABLE dbo.OrderLineItems ADD CONSTRAINT FK_OrderLineItems_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Customer')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Customer_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Customer ADD CONSTRAINT FK_Customer_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlements')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Settlements_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Settlements ADD CONSTRAINT FK_Settlements_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
END;

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlement')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Settlement_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Settlement ADD CONSTRAINT FK_Settlement_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
END;
"""


class FileTrackingService:
    """
    Service layer coordinator for source file registration and traceability.
    Delegates database operations to FileTrackingRepository and history queries to HistoryService.
    """

    def __init__(
        self,
        repo: Optional[FileTrackingRepository] = None,
        hist_service: Optional[HistoryService] = None
    ):
        self._repo = repo or file_tracking_repository
        self._history_service = hist_service or history_service

    @staticmethod
    def ensure_schema() -> None:
        """
        Idempotently verifies and applies the UploadedFiles table, FileID columns,
        and foreign key constraints on the database if necessary.
        """
        try:
            engine = get_engine()
            if engine.name == "mssql":
                with engine.begin() as conn:
                    conn.execute(text(SCHEMA_MIGRATION_SQL))
                logger.info("[FILE_TRACKING] Idempotent schema verification completed successfully.")
            elif engine.name in ("postgresql", "postgres"):
                pg_sql = """
                CREATE TABLE IF NOT EXISTS "UploadedFiles" (
                    "FileID" SERIAL PRIMARY KEY,
                    "FileName" VARCHAR(255) NOT NULL,
                    "UploadedAt" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    "Status" VARCHAR(30) NOT NULL DEFAULT 'Processing'
                );
                """
                with engine.begin() as conn:
                    conn.execute(text(pg_sql))
            elif engine.name == "sqlite":
                sqlite_sql = """
                CREATE TABLE IF NOT EXISTS UploadedFiles (
                    FileID INTEGER PRIMARY KEY AUTOINCREMENT,
                    FileName TEXT NOT NULL,
                    UploadedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                    Status TEXT NOT NULL DEFAULT 'Processing'
                );
                """
                with engine.begin() as conn:
                    conn.execute(text(sqlite_sql))
        except Exception as e:
            logger.warning(f"[FILE_TRACKING] Schema verification warning: {e}")

    def ensure_valid_file_id(self, file_id: Optional[int], file_name: str) -> int:
        """Ensures file_id actually exists in dbo.UploadedFiles; if not, creates one."""
        if file_id is not None:
            if self._repo.file_exists(file_id):
                return int(file_id)
        return self.create_uploaded_file(file_name)

    def create_uploaded_file(self, file_name: str) -> int:
        """
        Creates a new tracking record in dbo.UploadedFiles with status 'Processing'
        and returns the generated FileID.
        """
        return self._repo.create_file_record(file_name)

    def update_file_status(self, file_id: int, status: str) -> None:
        """
        Updates the status of a registered file ('Processing', 'Completed', 'Failed').
        """
        self._repo.update_file_status(file_id, status)

    def get_history_summary(self, limit: int = 200) -> Dict[str, Any]:
        """
        Retrieves comprehensive migration history across database dbo.UploadedFiles,
        background jobs ledger, session metadata, and sqlite duplicate tracker.
        """
        return self._history_service.get_history_summary(limit=limit)

    def get_uploaded_files(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves recent uploaded files with FileID, FileName, UploadedAt, Status,
        and enriched execution metadata.
        """
        return self._history_service.get_uploaded_files(limit=limit)

    def get_file_by_id(self, file_id: int) -> Optional[Dict[str, Any]]:
        """
        Retrieves a single file record by its FileID.
        """
        return self._repo.get_file_by_id(file_id)


file_tracking_service = FileTrackingService()

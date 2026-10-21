import datetime
from typing import List, Dict, Any, Optional
from sqlalchemy import text
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
    Manages source file registration and traceability tracking in dbo.UploadedFiles.
    """

    @staticmethod
    def ensure_schema() -> None:
        """
        Idempotently verifies and applies the UploadedFiles table, FileID columns,
        and foreign key constraints on the database.
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

    @staticmethod
    def ensure_valid_file_id(file_id: Optional[int], file_name: str) -> int:
        """Ensures file_id actually exists in dbo.UploadedFiles; if not, creates one."""
        if file_id is not None:
            try:
                engine = get_engine()
                with engine.begin() as conn:
                    tbl = "dbo.UploadedFiles" if engine.name == "mssql" else ('"UploadedFiles"' if engine.name in ("postgresql", "postgres") else "UploadedFiles")
                    fcol = "[FileID]" if engine.name == "mssql" else ('"FileID"' if engine.name in ("postgresql", "postgres") else "FileID")
                    exists = conn.execute(text(f"SELECT 1 FROM {tbl} WHERE {fcol} = :fid"), {"fid": file_id}).scalar()
                    if exists:
                        return int(file_id)
            except Exception:
                pass
        return FileTrackingService.create_uploaded_file(file_name)

    @staticmethod
    def create_uploaded_file(file_name: str) -> int:
        """
        Creates a new tracking record in dbo.UploadedFiles with status 'Processing'
        and returns the generated FileID.
        """
        engine = get_engine()
        file_name_clean = str(file_name or "uploaded_file.xlsx")[:255]

        with engine.begin() as conn:
            if engine.name == "mssql":
                stmt = text("""
                    INSERT INTO dbo.UploadedFiles (FileName, UploadedAt, Status)
                    OUTPUT INSERTED.FileID
                    VALUES (:file_name, SYSUTCDATETIME(), 'Processing')
                """)
                try:
                    res = conn.execute(stmt, {"file_name": file_name_clean})
                    file_id = res.scalar()
                except Exception as trigger_err:
                    err_str = str(trigger_err).lower()
                    if "trigger" in err_str or "output clause without into" in err_str:
                        fallback = text("""
                            INSERT INTO dbo.UploadedFiles (FileName, UploadedAt, Status)
                            VALUES (:file_name, SYSUTCDATETIME(), 'Processing');
                            SELECT SCOPE_IDENTITY();
                        """)
                        res = conn.execute(fallback, {"file_name": file_name_clean})
                        file_id = res.scalar()
                    else:
                        raise trigger_err
            elif engine.name in ("postgresql", "postgres"):
                stmt = text("""
                    INSERT INTO "UploadedFiles" ("FileName", "UploadedAt", "Status")
                    VALUES (:file_name, CURRENT_TIMESTAMP, 'Processing')
                    RETURNING "FileID"
                """)
                res = conn.execute(stmt, {"file_name": file_name_clean})
                file_id = res.scalar()
            else:
                stmt = text("""
                    INSERT INTO UploadedFiles (FileName, Status)
                    VALUES (:file_name, 'Processing')
                """)
                res = conn.execute(stmt, {"file_name": file_name_clean})
                file_id = getattr(res, "lastrowid", None) or conn.execute(text("SELECT last_insert_rowid()")).scalar()

        if file_id is None:
            raise RuntimeError(f"Could not generate FileID for uploaded file '{file_name_clean}'.")

        file_id_int = int(file_id)
        logger.info(f"[FILE_TRACKING] Registered uploaded file '{file_name_clean}' with FileID={file_id_int}")
        return file_id_int

    @staticmethod
    def update_file_status(file_id: int, status: str) -> None:
        """
        Updates the status of a registered file ('Processing', 'Completed', 'Failed').
        """
        try:
            engine = get_engine()
            table_name = "dbo.UploadedFiles" if engine.name == "mssql" else ("UploadedFiles" if engine.name == "sqlite" else '"UploadedFiles"')
            file_col = "FileID" if engine.name != "postgresql" else '"FileID"'
            status_col = "Status" if engine.name != "postgresql" else '"Status"'
            
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {table_name} SET {status_col} = :status WHERE {file_col} = :file_id"),
                    {"status": status, "file_id": file_id}
                )
            logger.info(f"[FILE_TRACKING] Updated FileID={file_id} status to '{status}'")
        except Exception as e:
            logger.error(f"[FILE_TRACKING] Failed to update FileID={file_id} status to '{status}': {e}")

    @staticmethod
    def get_uploaded_files(limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves recent uploaded files with FileID, FileName, UploadedAt, and Status.
        """
        engine = get_engine()
        table_name = "dbo.UploadedFiles" if engine.name == "mssql" else ("UploadedFiles" if engine.name == "sqlite" else '"UploadedFiles"')
        file_col = "FileID" if engine.name != "postgresql" else '"FileID"'
        limit_val = max(1, min(100, int(limit)))

        with engine.connect() as conn:
            if engine.name == "mssql":
                stmt = text(f"SELECT TOP (:limit) FileID, FileName, UploadedAt, Status FROM {table_name} ORDER BY FileID DESC")
            else:
                stmt = text(f"SELECT {file_col}, FileName, UploadedAt, Status FROM {table_name} ORDER BY {file_col} DESC LIMIT :limit")
            rows = conn.execute(stmt, {"limit": limit_val}).fetchall()

        results = []
        for r in rows:
            uploaded_at_val = r[2]
            if isinstance(uploaded_at_val, (datetime.datetime, datetime.date)):
                uploaded_at_str = uploaded_at_val.isoformat()
            else:
                uploaded_at_str = str(uploaded_at_val or "")
            results.append({
                "FileID": int(r[0]),
                "FileName": str(r[1]),
                "UploadedAt": uploaded_at_str,
                "Status": str(r[3])
            })
        return results

    @staticmethod
    def get_file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
        """
        Retrieves a single file record by its FileID.
        """
        engine = get_engine()
        table_name = "dbo.UploadedFiles" if engine.name == "mssql" else ("UploadedFiles" if engine.name == "sqlite" else '"UploadedFiles"')
        file_col = "FileID" if engine.name != "postgresql" else '"FileID"'

        with engine.connect() as conn:
            stmt = text(f"SELECT FileID, FileName, UploadedAt, Status FROM {table_name} WHERE {file_col} = :file_id")
            row = conn.execute(stmt, {"file_id": file_id}).fetchone()

        if not row:
            return None

        uploaded_at_val = row[2]
        if isinstance(uploaded_at_val, (datetime.datetime, datetime.date)):
            uploaded_at_str = uploaded_at_val.isoformat()
        else:
            uploaded_at_str = str(uploaded_at_val or "")

        return {
            "FileID": int(row[0]),
            "FileName": str(row[1]),
            "UploadedAt": uploaded_at_str,
            "Status": str(row[3])
        }


file_tracking_service = FileTrackingService()

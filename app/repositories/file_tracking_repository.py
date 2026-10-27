import datetime
from typing import Optional, List, Dict, Any, Callable
from sqlalchemy import text, Engine
from app.database.connection import get_engine
from app.core.logging import logger


class FileTrackingRepository:
    """
    Repository layer centralizing all database operations for dbo.UploadedFiles.
    Executes parameterized SQL queries using the project's existing connection pool
    and handles dialect-specific identity/limit conventions.
    """

    def __init__(self, engine_provider: Optional[Callable[[], Engine]] = None):
        self._engine_provider = engine_provider or get_engine

    def _get_engine(self) -> Engine:
        return self._engine_provider()

    def _table_name(self, engine: Engine) -> str:
        if engine.name == "mssql":
            return "dbo.UploadedFiles"
        elif engine.name in ("postgresql", "postgres"):
            return '"UploadedFiles"'
        return "UploadedFiles"

    def _col_name(self, col: str, engine: Engine) -> str:
        if engine.name == "mssql":
            return f"[{col}]"
        elif engine.name in ("postgresql", "postgres"):
            return f'"{col}"'
        return col

    def create_file_record(self, file_name: str) -> int:
        """
        Creates a new file record in UploadedFiles with status 'Processing'
        and returns the generated FileID.
        """
        engine = self._get_engine()
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
        logger.info(f"[FILE_TRACKING_REPO] Registered uploaded file '{file_name_clean}' with FileID={file_id_int}")
        return file_id_int

    def get_file_by_id(self, file_id: int) -> Optional[Dict[str, Any]]:
        """
        Retrieves a single file record by FileID.
        Uses NOLOCK on SQL Server to eliminate read contention during uploads.
        """
        engine = self._get_engine()
        tbl = self._table_name(engine)
        file_col = self._col_name("FileID", engine)

        with engine.connect() as conn:
            if engine.name == "mssql":
                stmt = text(f"SELECT TOP 1 {file_col}, FileName, UploadedAt, Status FROM {tbl} WITH (NOLOCK) WHERE {file_col} = :file_id")
            else:
                stmt = text(f"SELECT {file_col}, FileName, UploadedAt, Status FROM {tbl} WHERE {file_col} = :file_id LIMIT 1")
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

    def update_file_status(self, file_id: int, status: str) -> None:
        """
        Updates the Status column for a specific FileID.
        """
        engine = self._get_engine()
        tbl = self._table_name(engine)
        file_col = self._col_name("FileID", engine)
        status_col = self._col_name("Status", engine)

        with engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {tbl} SET {status_col} = :status WHERE {file_col} = :file_id"),
                {"status": status, "file_id": file_id}
            )
        logger.info(f"[FILE_TRACKING_REPO] Updated FileID={file_id} status to '{status}'")

    def file_exists(self, file_id: int) -> bool:
        """
        Checks whether a FileID exists in the UploadedFiles table.
        Uses TOP 1 with NOLOCK for minimum latency.
        """
        try:
            engine = self._get_engine()
            tbl = self._table_name(engine)
            file_col = self._col_name("FileID", engine)
            with engine.connect() as conn:
                if engine.name == "mssql":
                    stmt = text(f"SELECT TOP 1 1 FROM {tbl} WITH (NOLOCK) WHERE {file_col} = :fid")
                else:
                    stmt = text(f"SELECT 1 FROM {tbl} WHERE {file_col} = :fid LIMIT 1")
                res = conn.execute(stmt, {"fid": file_id}).scalar()
                return bool(res)
        except Exception:
            return False

    def get_total_count(self) -> int:
        """
        Returns total count of uploaded files using fast index count.
        """
        try:
            engine = self._get_engine()
            tbl = self._table_name(engine)
            with engine.connect() as conn:
                if engine.name == "mssql":
                    stmt = text(f"SELECT COUNT(1) FROM {tbl} WITH (NOLOCK)")
                else:
                    stmt = text(f"SELECT COUNT(1) FROM {tbl}")
                return int(conn.execute(stmt).scalar() or 0)
        except Exception as e:
            logger.warning(f"[FILE_TRACKING_REPO] Count query warning: {e}")
            return 0

    def list_files(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves recent uploaded files from UploadedFiles ordered by FileID DESC.
        Uses index seek/scan on clustered primary key with NOLOCK to prevent locking.
        """
        engine = self._get_engine()
        tbl = self._table_name(engine)
        file_col = self._col_name("FileID", engine)

        results: List[Dict[str, Any]] = []
        with engine.connect() as conn:
            if engine.name == "mssql":
                stmt = text(f"SELECT TOP (:limit) {file_col}, FileName, UploadedAt, Status FROM {tbl} WITH (NOLOCK) ORDER BY {file_col} DESC")
            else:
                stmt = text(f"SELECT {file_col}, FileName, UploadedAt, Status FROM {tbl} ORDER BY {file_col} DESC LIMIT :limit")

            for r in conn.execute(stmt, {"limit": limit}).fetchall():
                fid = int(r[0])
                uploaded_at_val = r[2]
                if isinstance(uploaded_at_val, (datetime.datetime, datetime.date)):
                    uploaded_at_str = uploaded_at_val.isoformat()
                else:
                    uploaded_at_str = str(uploaded_at_val or "")

                results.append({
                    "FileID": fid,
                    "FileName": str(r[1]),
                    "UploadedAt": uploaded_at_str,
                    "Status": str(r[3])
                })

        return results


file_tracking_repository = FileTrackingRepository()

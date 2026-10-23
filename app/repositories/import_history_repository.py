import os
import sqlite3
import threading
from typing import Optional, List, Dict, Any
from app.core.logging import logger

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "uploads")
IMPORT_HISTORY_DB = os.path.join(os.path.dirname(UPLOAD_DIR), "data", "import_history.sqlite3")


class ImportHistoryRepository:
    """
    Repository layer centralizing content-fingerprint ledger operations in import_history.sqlite3.
    Thread-safe and isolates direct sqlite3 queries.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or IMPORT_HISTORY_DB
        self._lock = threading.Lock()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            with sqlite3.connect(self._db_path, timeout=30) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS successful_file_imports (
                        file_sha256 TEXT PRIMARY KEY,
                        original_filename TEXT NOT NULL,
                        upload_id TEXT NOT NULL,
                        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
            self._initialized = True

    def find_by_sha256(self, file_sha256: str) -> Optional[Dict[str, Any]]:
        """
        Looks up a previous successful import by content SHA-256 fingerprint.
        """
        if not file_sha256 or not os.path.exists(self._db_path):
            return None

        self._ensure_initialized()
        with self._lock, sqlite3.connect(self._db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT original_filename, upload_id, imported_at "
                "FROM successful_file_imports WHERE file_sha256 = ?",
                (file_sha256,)
            ).fetchone()
            return dict(row) if row else None

    def record_import(self, file_sha256: str, original_filename: str, upload_id: str) -> None:
        """
        Registers a successfully completed import in the content ledger.
        """
        if not file_sha256 or not upload_id:
            return

        self._ensure_initialized()
        with self._lock, sqlite3.connect(self._db_path, timeout=30) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO successful_file_imports "
                "(file_sha256, original_filename, upload_id) VALUES (?, ?, ?)",
                (file_sha256, original_filename or "", upload_id)
            )
            conn.commit()
        logger.info(f"[IMPORT_HISTORY_REPO] Recorded successful import for upload_id={upload_id}")

    def list_successful_imports(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Retrieves list of successful imports ordered by imported_at DESC with limit.
        """
        if not os.path.exists(self._db_path):
            return []

        self._ensure_initialized()
        with self._lock, sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT file_sha256, original_filename, upload_id, imported_at "
                "FROM successful_file_imports ORDER BY imported_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]


import_history_repository = ImportHistoryRepository()

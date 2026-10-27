import os
import glob
import json
import datetime
from typing import List, Dict, Any, Optional

from app.repositories.file_tracking_repository import FileTrackingRepository, file_tracking_repository
from app.repositories.import_history_repository import ImportHistoryRepository, import_history_repository
from app.core.logging import logger


class HistoryService:
    """
    Service layer handling upload history, migration history aggregation,
    and status reporting across database tracking, background jobs, and session storage.
    """

    def __init__(
        self,
        tracking_repo: Optional[FileTrackingRepository] = None,
        import_history_repo: Optional[ImportHistoryRepository] = None
    ):
        self._tracking_repo = tracking_repo or file_tracking_repository
        self._import_history_repo = import_history_repo or import_history_repository

    def get_history_summary(self, limit: int = 200) -> Dict[str, Any]:
        """
        Retrieves migration history directly from database dbo.UploadedFiles via FileTrackingRepository.
        Strictly reflects records from dbo.UploadedFiles.
        """
        try:
            db_files = self._tracking_repo.list_files(limit=limit)
            total_count = self._tracking_repo.get_total_count()
        except Exception as e:
            logger.error(f"[HISTORY_SERVICE] Error reading from UploadedFiles table: {e}")
            db_files = []
            total_count = 0

        items: List[Dict[str, Any]] = []
        for r in db_files:
            items.append({
                "fileId": r["FileID"],
                "uploadId": None,
                "fileName": r["FileName"],
                "uploadedAt": r["UploadedAt"],
                "status": r["Status"],
                "totalRows": None,
                "totalInserted": None,
                "totalReused": None,
                "source": "database"
            })

        completed_cnt = sum(1 for x in items if str(x.get("status")).capitalize() == "Completed")
        failed_cnt = sum(1 for x in items if str(x.get("status")).capitalize() == "Failed")
        processing_cnt = sum(1 for x in items if str(x.get("status")).capitalize() == "Processing")

        total_files = total_count if (isinstance(total_count, int) and total_count > len(items)) else len(items)

        return {
            "totalFiles": total_files,
            "completedCount": completed_cnt,
            "failedCount": failed_cnt,
            "processingCount": processing_cnt,
            "totalRowsMigrated": 0,
            "files": items
        }

    def get_uploaded_files(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves recent uploaded files with FileID, FileName, UploadedAt, Status,
        and enriched execution metadata.
        """
        summary = self.get_history_summary(limit=limit)
        results = []
        for item in summary.get("files", []):
            results.append({
                "FileID": item.get("fileId"),
                "FileName": item.get("fileName"),
                "UploadedAt": item.get("uploadedAt"),
                "Status": item.get("status"),
                "TotalRows": item.get("totalRows"),
                "TotalInserted": item.get("totalInserted"),
                "UploadID": item.get("uploadId")
            })
        return results


history_service = HistoryService()

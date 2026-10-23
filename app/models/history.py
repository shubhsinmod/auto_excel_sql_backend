from typing import List, Optional, Any
from pydantic import BaseModel, Field


class UploadedFileRecord(BaseModel):
    file_id: int
    file_name: str
    uploaded_at: str
    status: str


class HistoryFileItem(BaseModel):
    fileId: Any
    uploadId: Optional[str] = None
    fileName: str
    uploadedAt: str
    status: str
    totalRows: Optional[int] = None
    totalInserted: Optional[int] = None
    totalReused: Optional[int] = None
    source: Optional[str] = None


class HistorySummaryResponse(BaseModel):
    totalFiles: int
    completedCount: int
    failedCount: int
    processingCount: int
    totalRowsMigrated: int
    files: List[HistoryFileItem] = Field(default_factory=list)

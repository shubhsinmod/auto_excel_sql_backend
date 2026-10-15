from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class ExcelAnalysisResponse(BaseModel):
    """
    Metadata and sample data returned after analyzing an uploaded Excel file.
    """
    upload_id: str = Field(..., alias="uploadId")
    file_name: str = Field(..., alias="fileName")
    sheet_name: str = Field(..., alias="sheetName")
    available_sheets: List[str] = Field(default_factory=list, alias="availableSheets")
    row_count: int = Field(..., alias="rowCount")
    column_count: int = Field(..., alias="columnCount")
    columns: List[str] = Field(default_factory=list)
    sample_rows: List[Dict[str, Any]] = Field(default_factory=list, alias="sampleRows")
    file_size_bytes: int = Field(0, alias="fileSizeBytes")
    created_at: str = Field(..., alias="createdAt")
    header_row_number: int = Field(1, alias="headerRowNumber")
    title_rows_skipped: List[int] = Field(default_factory=list, alias="titleRowsSkipped")
    header_detection_method: str = Field("STRUCTURAL_ANALYSIS", alias="headerDetectionMethod")
    original_column_count: Optional[int] = Field(None, alias="originalColumnCount")
    removed_column_count: int = Field(0, alias="removedColumnCount")
    removed_columns: List[Dict[str, Any]] = Field(default_factory=list, alias="removedColumns")
    missing_header_columns: List[Dict[str, Any]] = Field(default_factory=list, alias="missingHeaderColumns")

    class Config:
        populate_by_name = True
        by_alias = True


class ExcelUploadErrorResponse(BaseModel):
    """
    Standardized error response for Excel validation failures.
    """
    success: bool = False
    error_code: str = Field(..., alias="errorCode")
    message: str

    class Config:
        populate_by_name = True
        by_alias = True

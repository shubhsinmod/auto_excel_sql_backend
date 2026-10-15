import os
from typing import Tuple, Optional
from app.core.logging import logger

ALLOWED_EXTENSIONS = {
    ".xlsx", ".xls", ".xlsm", ".xlsb",
    ".csv", ".tsv", ".txt",
    ".json", ".jsonl",
    ".xml", ".parquet"
}
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB limit


def validate_excel_file_extension(filename: str) -> Tuple[bool, Optional[str]]:
    """
    Validates file extension. Allows all file types.
    """
    if not filename:
        return False, "No file provided."
    
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        return False, "Uploaded file has no extension."
    
    return True, None


def validate_excel_workbook(file_path: str) -> Tuple[bool, Optional[str]]:
    """
    Inspects file on disk to ensure it is a valid non-empty file.
    """
    if not os.path.exists(file_path):
        return False, "Uploaded file was not found on server."
    
    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return False, "The uploaded Excel file is empty (0 bytes)."
    
    if file_size > MAX_FILE_SIZE_BYTES:
        return False, f"File size exceeds maximum allowed limit of {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB."

    return True, None

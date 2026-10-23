import os
import uuid
import json
import time
import shutil
import hashlib
from typing import Dict, Any, Optional
from fastapi import UploadFile

from app.excel.validator import validate_excel_file_extension, validate_excel_workbook
from app.excel.analyzer import analyze_excel_file
from app.repositories.file_tracking_repository import file_tracking_repository
from app.repositories.import_history_repository import import_history_repository
from app.core.logging import logger

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


class ExcelService:
    """
    Service layer handling Excel upload, validation, session storage, and retrieval with timing instrumentation.
    Delegates database file tracking and ledger operations to dedicated repositories.
    """

    @staticmethod
    def save_and_analyze_excel(file: UploadFile) -> Dict[str, Any]:
        """
        Receives an uploaded Excel file, validates extension & workbook content,
        saves it temporarily with a unique uploadId, and returns full dynamic analysis.
        """
        t_start = time.time()
        logger.info("[STEP3] START")

        original_filename = file.filename or "uploaded_file.xlsx"

        # 1. Validate file extension
        is_valid_ext, ext_error = validate_excel_file_extension(original_filename)
        if not is_valid_ext:
            raise ValueError(ext_error)

        # 2. Register file tracking in database via repository to generate unique FileID
        file_id = None
        try:
            file_id = file_tracking_repository.create_file_record(original_filename)
        except Exception as ft_err:
            logger.error(f"[FILE_TRACKING] Failed to register file '{original_filename}': {ft_err}")
            raise

        # 3. Generate unique upload session ID
        upload_id = f"upload_{uuid.uuid4().hex[:12]}"
        safe_ext = os.path.splitext(original_filename)[1].lower()
        target_filename = f"{upload_id}{safe_ext}"
        target_file_path = os.path.join(UPLOAD_DIR, target_filename)

        logger.info("[STEP3] Excel file save START")
        t_save_0 = time.time()
        try:
            with open(target_file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        except Exception as e:
            logger.error(f"Failed to write uploaded file to disk: {str(e)}")
            raise IOError("Could not save uploaded file on server.")
        finally:
            file.file.close()
        t_save_1 = time.time()
        logger.info(f"[STEP3] Excel file save END | {t_save_1 - t_save_0:.3f}s")

        # 3.5 Content fingerprint calculation for session tracking
        file_sha256 = ExcelService._sha256_file(target_file_path)

        # 4. Validate saved Excel workbook
        is_valid_wb, wb_error = validate_excel_workbook(target_file_path)
        if not is_valid_wb:
            if os.path.exists(target_file_path):
                os.remove(target_file_path)
            raise ValueError(wb_error)

        # 5. Perform dynamic structure analysis
        logger.info("[STEP3] Excel analysis START")
        t_an_0 = time.time()
        try:
            analysis_data = analyze_excel_file(
                file_path=target_file_path,
                original_filename=original_filename,
                upload_id=upload_id
            )
            analysis_data["fileId"] = file_id
        except Exception as e:
            if os.path.exists(target_file_path):
                os.remove(target_file_path)
            logger.error(f"Failed to analyze Excel file {original_filename}: {str(e)}")
            raise ValueError(f"Could not parse Excel workbook structure: {str(e)}")
        t_an_1 = time.time()
        logger.info(f"[STEP3] Excel analysis END | {t_an_1 - t_an_0:.3f}s")

        # 6. Persist upload session metadata for Step 3 processing
        session_file_path = os.path.join(UPLOAD_DIR, f"{upload_id}_session.json")
        session_payload = {
            **analysis_data,
            "filePath": target_file_path,
            "fileSha256": file_sha256,
            "fileId": file_id
        }
        with open(session_file_path, "w", encoding="utf-8") as f:
            json.dump(session_payload, f, indent=2)

        t_end = time.time()
        logger.info(f"[STEP3] Upload & Analysis COMPLETE | TOTAL={t_end - t_start:.3f}s")

        return analysis_data

    @staticmethod
    def _sha256_file(file_path: str) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def find_successful_import(file_sha256: str) -> Optional[Dict[str, Any]]:
        """Finds a previously imported file by SHA-256 using the repository."""
        return import_history_repository.find_by_sha256(file_sha256)

    @staticmethod
    def mark_import_success(upload_id: str) -> None:
        """Records the content hash in the ledger repository after the SQL transaction commits."""
        session = ExcelService.get_upload_session(upload_id)
        if not session:
            logger.warning(f"Cannot register successful import: session {upload_id} not found")
            return
        file_sha256 = session.get("fileSha256")
        if not file_sha256:
            logger.warning(f"Cannot register successful import: file hash missing for {upload_id}")
            return
        import_history_repository.record_import(
            file_sha256=file_sha256,
            original_filename=session.get("fileName") or "uploaded_file",
            upload_id=upload_id
        )

    @staticmethod
    def get_upload_session(upload_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves upload session metadata by uploadId for Step 3 processing.
        """
        session_file_path = os.path.join(UPLOAD_DIR, f"{upload_id}_session.json")
        if not os.path.exists(session_file_path):
            return None
        
        try:
            with open(session_file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read upload session {upload_id}: {str(e)}")
            return None


excel_service = ExcelService()

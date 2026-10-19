import io
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app
from app.services.excel_service import ExcelService

client = TestClient(app)


def test_duplicate_upload_rejected_with_409():
    """Verify that uploading an identical file that was already successfully imported returns HTTP 409."""
    file_bytes = b"header1,header2\nval1,val2\n"
    file_content = io.BytesIO(file_bytes)

    with patch("app.services.excel_service.ExcelService.find_successful_import") as mock_find:
        mock_find.return_value = {
            "original_filename": "previous_file.csv",
            "imported_at": "2026-10-18 20:00:00",
            "upload_id": "upload_prev_123"
        }

        response = client.post(
            "/api/excel/upload",
            files={"file": ("test.csv", file_content, "text/csv")}
        )

        assert response.status_code == 409
        assert "This exact file was already imported successfully" in response.json()["detail"]


def test_unique_upload_allowed():
    """Verify that uploading a new file proceeds normally."""
    file_bytes = b"colA,colB\n1,2\n"
    file_content = io.BytesIO(file_bytes)

    with patch("app.services.excel_service.ExcelService.find_successful_import", return_value=None), \
         patch("app.excel.validator.validate_excel_workbook", return_value=(True, None)), \
         patch("app.excel.analyzer.analyze_excel_file") as mock_analyze:

        mock_analyze.return_value = {
            "uploadId": "upload_new_test",
            "fileName": "test.csv",
            "fileSize": len(file_bytes),
            "sheets": ["Sheet1"],
            "summary": {"totalRows": 1, "totalColumns": 2},
            "columns": [],
            "sampleRows": [],
            "suggestedMappings": [],
            "validationRules": []
        }

        response = client.post(
            "/api/excel/upload",
            files={"file": ("test.csv", file_content, "text/csv")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["uploadId"].startswith("upload_")
        assert data["fileName"] == "test.csv"

from fastapi import APIRouter, UploadFile, File, HTTPException, status
from app.services.excel_service import excel_service
from app.models.excel import ExcelAnalysisResponse
from app.core.logging import logger

router = APIRouter(prefix="/api/excel", tags=["Excel"])


@router.post("/upload", response_model=ExcelAnalysisResponse)
async def upload_excel(file: UploadFile = File(...)):
    """
    Upload and analyze Excel spreadsheet (.xlsx, .xls).
    Validates file extension, integrity, worksheets, headers, and returns dynamic metadata.
    """
    if not file or not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file was selected for upload."
        )

    try:
        analysis_result = excel_service.save_and_analyze_excel(file)
        return analysis_result
    except ValueError as val_err:
        logger.warning(f"Excel validation error for file '{file.filename}': {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"Unexpected error uploading Excel file '{file.filename}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred while processing the Excel file: {str(e)}"
        )


@router.get("/{upload_id}")
async def get_excel_session(upload_id: str):
    """
    Retrieves stored Excel session metadata by upload ID for Step 3 processing.
    """
    session = excel_service.get_upload_session(upload_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Upload session '{upload_id}' not found or expired."
        )
    return session

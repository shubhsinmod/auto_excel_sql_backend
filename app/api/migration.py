import time
from fastapi import APIRouter, HTTPException, status, Query, Response
from app.services.migration_service import migration_service
from app.models.migration import (
    Step3ProcessingResponse,
    MigrationExecutionResponse,
    MigrationJobStatus,
    ValidationSummary,
    MappingOverridesRequest,
    ValueOverridesRequest
)
from app.core.logging import logger

router = APIRouter(prefix="/api/migration", tags=["Migration"])

@router.get("/files")
def list_uploaded_files(limit: int = 50):
    """
    Returns list of uploaded files and their migration status.
    """
    from app.services.file_tracking_service import file_tracking_service
    return file_tracking_service.get_uploaded_files(limit=limit)




@router.post("/values/{upload_id}", response_model=Step3ProcessingResponse)
def update_value_mappings(upload_id: str, request: ValueOverridesRequest):
    """
    Saves user-confirmed or overridden value mappings, optionally caches them for future imports,
    immediately re-runs validation and preview generation, and returns the updated preview.
    """
    t0 = time.time()
    logger.info(f"[API] UPDATE VALUE MAPPINGS START | POST /api/migration/values/{upload_id}")
    try:
        result = migration_service.save_value_overrides(upload_id, request.overrides)
        t1 = time.time()
        logger.info(f"[API] UPDATE VALUE MAPPINGS END | {t1 - t0:.3f}s")
        return result
    except ValueError as val_err:
        logger.warning(f"[API] Value mapping update error for uploadId '{upload_id}': {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Value mapping update error for uploadId '{upload_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while updating value mappings: {str(e)}"
        )


@router.post("/mappings/{upload_id}", response_model=Step3ProcessingResponse)
def update_column_mappings(upload_id: str, request: MappingOverridesRequest):
    """
    Saves user-modified column mappings or confirmations, immediately re-runs
    validation and preview generation, and returns the updated preview.
    """
    t0 = time.time()
    logger.info(f"[API] UPDATE MAPPINGS START | POST /api/migration/mappings/{upload_id}")
    try:
        result = migration_service.save_column_overrides(upload_id, request.overrides)
        t1 = time.time()
        logger.info(f"[API] UPDATE MAPPINGS END | {t1 - t0:.3f}s")
        return result
    except ValueError as val_err:
        logger.warning(f"[API] Mapping update error for uploadId '{upload_id}': {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Mapping update error for uploadId '{upload_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while updating column mappings: {str(e)}"
        )



@router.post("/process/{upload_id}", response_model=Step3ProcessingResponse)
def process_migration_step3(upload_id: str, force_refresh: bool = False):
    """
    Executes Step 1 & Step 2 Intelligent Preview & Validation for an uploaded Excel session:
    Schema discovery -> Column Mapping -> FK & Value Matching -> Complete Row Preview -> Validation summary.
    Performs ZERO database writes.
    """
    t0 = time.time()
    logger.info(f"[API] STEP3 HANDLER START | POST /api/migration/process/{upload_id}")
    try:
        result = migration_service.process_step3(upload_id, force_refresh=force_refresh)
        t1 = time.time()
        logger.info(f"[API] STEP3 HANDLER END | {t1 - t0:.3f}s")
        return result
    except ValueError as val_err:
        logger.warning(f"[STAGE] PREVIEW_FAILED | uploadId={upload_id} | error={str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[STAGE] PREVIEW_FAILED | uploadId={upload_id} | error={str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during Step 3 preview processing: {str(e)}"
        )


@router.post("/process", response_model=Step3ProcessingResponse)
def process_migration_step3_query(uploadId: str = Query(...)):
    """
    Query parameter alias for Step 3 processing endpoint.
    """
    return process_migration_step3(uploadId)


@router.get("/preview/{upload_id}", response_model=Step3ProcessingResponse)
def get_migration_preview(upload_id: str):
    """
    Retrieves the complete migration preview and validation plan for an uploaded session.
    Never executes INSERT, UPDATE, or DELETE statements.
    """
    return process_migration_step3(upload_id)


@router.post("/validate/{upload_id}", response_model=ValidationSummary)
def validate_dataset(upload_id: str):
    """
    Explicitly triggers full dataset validation across all source rows and returns validation summary.
    """
    res = migration_service.process_step3(upload_id)
    if res.validation_summary:
        return res.validation_summary
    raise HTTPException(status_code=500, detail="Validation summary could not be generated.")


@router.post("/start/{upload_id}", response_model=MigrationJobStatus)
def start_migration(
    upload_id: str,
    duplicate_action: str = Query("override", alias="duplicateAction")
):
    """
    Starts asynchronous database migration in background worker thread and promptly returns job status.
    Safe against concurrent duplicate clicks or reloads.
    """
    t0 = time.time()
    logger.info(f"[API] START MIGRATION HANDLER | POST /api/migration/start/{upload_id} | action={duplicate_action}")
    try:
        job = migration_service.start_migration(upload_id, duplicate_action=duplicate_action)
        t1 = time.time()
        logger.info(f"[API] START MIGRATION HANDLER END | status={job.status} | {t1 - t0:.3f}s")
        return job
    except ValueError as val_err:
        logger.warning(f"[API] Start Migration Error for uploadId '{upload_id}': {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Start Migration Error for uploadId '{upload_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while starting database migration: {str(e)}"
        )


@router.get("/status/{upload_id}", response_model=MigrationJobStatus)
def get_migration_status(upload_id: str):
    """
    Polls real-time progress and status of an ongoing or completed migration job.
    Never blocks or times out; returns current snapshot of migration job.
    """
    try:
        return migration_service.get_migration_status(upload_id)
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Get Migration Status Error for uploadId '{upload_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while checking migration status: {str(e)}"
        )


@router.post("/reconcile/{upload_id}", response_model=MigrationJobStatus)
def reconcile_migration(upload_id: str):
    """
    Reconciles uncertain transaction outcome with SQL Server database state
    after network drops, client timeout, or server restart.
    """
    t0 = time.time()
    logger.info(f"[API] RECONCILE HANDLER START | POST /api/migration/reconcile/{upload_id}")
    try:
        job = migration_service.reconcile_migration(upload_id)
        t1 = time.time()
        logger.info(f"[API] RECONCILE HANDLER END | status={job.status} | {t1 - t0:.3f}s")
        return job
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Reconcile Error for uploadId '{upload_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while reconciling migration status: {str(e)}"
        )


@router.post("/execute/{upload_id}", response_model=MigrationExecutionResponse)
def execute_migration(
    upload_id: str,
    duplicate_action: str = Query("override", alias="duplicateAction")
):
    """
    Executes actual parameterized SQL database migration for an uploaded Excel session inside a single atomic transaction.
    """
    t0 = time.time()
    logger.info(f"[API] EXECUTE HANDLER START | POST /api/migration/execute/{upload_id} | action={duplicate_action}")
    try:
        result = migration_service.execute_migration(upload_id, duplicate_action=duplicate_action)
        t1 = time.time()
        logger.info(f"[API] EXECUTE HANDLER END | {t1 - t0:.3f}s")
        return result
    except ValueError as val_err:
        logger.warning(f"[API] Migration Execution Error for uploadId '{upload_id}': {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Migration Execution Error for uploadId '{upload_id}': {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during database migration: {str(e)}"
        )


@router.post("/execute", response_model=MigrationExecutionResponse)
def execute_migration_query(uploadId: str = Query(...)):
    """
    Query parameter alias for migration execution endpoint.
    """
    return execute_migration(uploadId)


@router.get("/report/{upload_id}")
def download_error_report(upload_id: str):
    """
    Generates and downloads a CSV audit/error report for the migration session.
    """
    csv_data = migration_service.get_error_report_csv(upload_id)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=migration_report_{upload_id}.csv"}
    )


@router.post("/dry-run/mock")
def execute_dry_run_mock():
    """
    Executes a complete migration dry-run test using generated representative mock data.
    ZERO database writes occur. Never inserts, updates, or deletes records.
    """
    t0 = time.time()
    logger.info("[API] DRY-RUN MOCK START | POST /api/migration/dry-run/mock")
    try:
        report = migration_service.run_dry_run_mock()
        t1 = time.time()
        logger.info(f"[API] DRY-RUN MOCK COMPLETED in {t1 - t0:.3f}s | Status={report.final_status}")
        return report
    except Exception as e:
        logger.error(f"[API] Dry-run mock error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during dry-run testing: {str(e)}"
        )


@router.post("/dry-run/{upload_id}")
def execute_dry_run_upload(upload_id: str):
    """
    Executes a complete migration dry-run test using actual uploaded Excel/CSV data.
    ZERO database writes occur. Never inserts, updates, or deletes records.
    """
    t0 = time.time()
    logger.info(f"[API] DRY-RUN UPLOAD START | POST /api/migration/dry-run/{upload_id}")
    try:
        report = migration_service.run_dry_run_upload(upload_id)
        t1 = time.time()
        logger.info(f"[API] DRY-RUN UPLOAD COMPLETED in {t1 - t0:.3f}s | Status={report.final_status}")
        return report
    except ValueError as val_err:
        logger.warning(f"[API] Dry-run upload validation error: {val_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"[API] Dry-run upload error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during dry-run testing: {str(e)}"
        )


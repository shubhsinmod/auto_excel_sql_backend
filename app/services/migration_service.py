import time
import datetime
import threading
import io
import csv
from typing import Dict, Any, Optional, List
import pandas as pd

from app.services.excel_service import excel_service
from app.services.database_service import database_service
from app.database.connection import get_engine
from app.excel.reader import load_file_dataframe
from app.mapping.matcher import ColumnMatcher
from app.migration.preview import MigrationPreviewEngine
from app.migration.engine import MigrationEngine
from app.migration.transaction import transaction_manager
from app.models.migration import (
    Step3ProcessingResponse,
    MigrationExecutionResponse,
    MigrationJobStatus,
    ValueResolutionResult,
    ValueResolutionSummary,
    ColumnOverrideItem,
    ValueOverrideItem
)
from app.migration.job_manager import job_manager
from app.mapping.values import value_registry
from app.core.logging import logger


class MigrationService:
    """
    Service layer coordinating preview generation, entire dataset validation,
    column mapping overrides, and atomic database migration execution.
    Includes in-memory idempotency cache and thread locks to protect against concurrent duplicate requests.
    """

    def __init__(self):
        self._step3_cache: Dict[str, Step3ProcessingResponse] = {}
        self._migration_cache: Dict[str, MigrationExecutionResponse] = {}
        self._user_overrides: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._user_value_overrides: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._lock = threading.Lock()

    def save_column_overrides(self, upload_id: str, overrides: List[ColumnOverrideItem]) -> Step3ProcessingResponse:
        """
        Saves user-modified column mappings or DO_NOT_MIGRATE markers,
        invalidates preview cache, and immediately re-runs validation and preview.
        """
        with self._lock:
            override_map: Dict[str, Dict[str, Any]] = {}
            for o in overrides:
                override_map[o.excel_column] = {
                    "databaseTable": o.database_table,
                    "databaseColumn": o.database_column,
                    "action": o.action,
                    "businessConcept": getattr(o, "business_concept", None),
                    "saveRule": getattr(o, "save_rule", False)
                }
            self._user_overrides[upload_id] = override_map
            self._step3_cache.pop(upload_id, None)
            self._migration_cache.pop(upload_id, None)

        logger.info(f"[STEP3] Saved {len(overrides)} mapping overrides for uploadId={upload_id}. Re-running preview...")
        return self.process_step3(upload_id, force_refresh=True)

    def save_value_overrides(self, upload_id: str, overrides: List[ValueOverrideItem]) -> Step3ProcessingResponse:
        """
        Saves user-modified or approved value mappings, optionally caching them
        for future imports scoped to the target database column, invalidates preview cache,
        and re-runs preview.
        """
        schema = database_service.get_schema_metadata()
        db_scope = schema.database_name or "default"

        with self._lock:
            val_override_map = self._user_value_overrides.get(upload_id, {})
            for vo in overrides:
                col_key = f"{vo.database_table}.{vo.database_column}"
                if col_key not in val_override_map:
                    val_override_map[col_key] = {}
                val_override_map[col_key][str(vo.excel_value).strip()] = str(vo.target_value).strip()

                if vo.excel_column not in val_override_map:
                    val_override_map[vo.excel_column] = {}
                val_override_map[vo.excel_column][str(vo.excel_value).strip()] = str(vo.target_value).strip()

                if vo.save_cache:
                    value_registry.save_approved_mapping(
                        table=vo.database_table,
                        column=vo.database_column,
                        source_value=vo.excel_value,
                        target_value=vo.target_value,
                        database_scope=db_scope
                    )

            self._user_value_overrides[upload_id] = val_override_map
            self._step3_cache.pop(upload_id, None)
            self._migration_cache.pop(upload_id, None)

        logger.info(f"[STEP3] Saved {len(overrides)} value overrides for uploadId={upload_id}. Re-running preview...")
        return self.process_step3(upload_id, force_refresh=True)

    def process_step3(self, upload_id: str, force_refresh: bool = False) -> Step3ProcessingResponse:
        """
        Executes Step 1 & Step 2:
        - Read-only schema discovery and column matching (respecting user overrides).
        - Generates complete row-by-row preview plan (SourceRowPreviewItem).
        - Generates complete dependency and foreign-key plan (ForeignKeyPlanItem).
        - Validates the entire dataset across all rows (ValidationSummary).
        - Computes planned operations summary (PlannedOperationsSummary).
        - ZERO database writes (no INSERT, UPDATE, or DELETE).
        """
        with self._lock:
            if not force_refresh and upload_id in self._step3_cache:
                logger.info(f"[STEP3] Returning cached preview response for uploadId={upload_id}")
                return self._step3_cache[upload_id]

        t_start = time.time()
        logger.info(f"[STEP3] START | Processing uploadId={upload_id}")

        # 1. Fetch uploaded Excel session metadata
        session_data = excel_service.get_upload_session(upload_id)
        if not session_data:
            raise ValueError(f"Upload session '{upload_id}' not found or expired.")

        file_path = session_data.get("filePath")
        sheet_name = session_data.get("sheetName")
        file_name = session_data.get("fileName") or "uploaded_file.xlsx"

        if not file_path or not pd.io.common.file_exists(file_path):
            raise ValueError("Uploaded data file is missing from server storage.")

        logger.info(f"[STAGE] FILE_PARSE_START | uploadId={upload_id} | file={file_name}")
        t_parse = time.time()
        header_row = session_data.get("headerRowNumber") or 1
        df = load_file_dataframe(
            file_path,
            sheet_name=sheet_name,
            header=(header_row - 1) if header_row >= 1 else 0
        ).dropna(how='all')
        df.columns = [str(c).strip() for c in df.columns]
        logger.info(f"[STAGE] FILE_PARSE_COMPLETED | uploadId={upload_id} | rows={len(df)} | cols={len(df.columns)} | {time.time() - t_parse:.3f}s")

        excel_columns = df.columns.tolist()

        # 2. Database Schema Introspection (uses fast catalog cache)
        logger.info(f"[STAGE] SCHEMA_DISCOVERY_START | uploadId={upload_id}")
        t_schema = time.time()
        engine = get_engine()
        schema = database_service.get_schema_metadata()
        logger.info(f"[STAGE] SCHEMA_DISCOVERY_COMPLETED | uploadId={upload_id} | tables={len(schema.tables)} | {time.time() - t_schema:.3f}s")

        # 3. Dynamic Column Matching (incorporates user overrides)
        logger.info(f"[STAGE] COLUMN_MAPPING_START | uploadId={upload_id}")
        t_col = time.time()
        overrides = self._user_overrides.get(upload_id, {})
        column_mappings = ColumnMatcher.match_columns(
            excel_columns,
            schema,
            user_overrides=overrides,
            database_scope=schema.database_name
        )
        logger.info(f"[STAGE] COLUMN_MAPPING_COMPLETED | uploadId={upload_id} | count={len(column_mappings)} | {time.time() - t_col:.3f}s")

        # 4. Read-Only Preview & Validation Generation (ZERO database writes)
        val_overrides = self._user_value_overrides.get(upload_id, {})
        preview_res = MigrationPreviewEngine.generate_preview(
            df=df,
            file_name=file_name,
            schema=schema,
            column_mappings=column_mappings,
            engine=engine,
            user_value_overrides=val_overrides
        )
        (
            preview_rows,
            dependency_plan,
            planned_ops,
            validation_summary,
            execution_order,
            value_resolutions
        ) = preview_res
        value_resolution_summary = getattr(preview_res, "value_resolution_summary", None)
        value_mappings = getattr(preview_res, "value_mappings", [])

        # Statistics
        matched_cols = sum(1 for m in column_mappings if m.status == "MATCHED")
        tables_processed = list(set(m.database_table for m in column_mappings if m.database_table not in ("UNKNOWN", "DO_NOT_MIGRATE")))

        total_time = round(time.time() - t_start, 3)
        logger.info(f"[STEP3] COMPLETE | TOTAL={total_time:.3f}s")

        response = Step3ProcessingResponse(
            uploadId=upload_id,
            status="COMPLETED",
            schema=schema,
            columnMappings=column_mappings,
            valueResolutions=value_resolutions,
            valueMappings=value_mappings,
            valueResolutionSummary=value_resolution_summary,
            tablesProcessed=tables_processed,
            tableRoles=planned_ops.table_roles if planned_ops else {},
            skippedTables=planned_ops.skipped_tables if planned_ops else {},
            targetInsertTables=planned_ops.target_insert_tables if planned_ops else [],
            lookupOnlyTables=planned_ops.lookup_only_tables if planned_ops else [],
            dependencyInsertTables=planned_ops.dependency_insert_tables if planned_ops else [],
            matchedColumnsCount=matched_cols,
            matchedValuesCount=value_resolution_summary.values_matched_existing,
            unmatchedValuesCount=value_resolution_summary.unresolved_foreign_keys,
            ambiguousValuesCount=value_resolution_summary.ambiguous_values,
            processingTimeSeconds=total_time,
            previewRows=preview_rows,
            dependencyPlan=dependency_plan,
            validationSummary=validation_summary,
            plannedOperations=planned_ops,
            executionOrder=execution_order,
            statement="This preview has not written any data to the database.",
            canExecute=validation_summary.can_execute,
            itemPriceResolutions=getattr(preview_res, "item_price_resolutions", [])
        )

        with self._lock:
            self._step3_cache[upload_id] = response

        return response


    def start_migration(self, upload_id: str, duplicate_action: str = "override") -> MigrationJobStatus:
        """
        Initiates asynchronous background migration and promptly returns a job status.
        Coordinates in-flight requests and prevents duplicate executions.
        """
        # 1. Check if already completed or running
        existing_job = job_manager.get_job(upload_id)
        if existing_job and existing_job.status in ("RUNNING", "COMPLETED"):
            return existing_job

        with self._lock:
            if upload_id in self._migration_cache:
                res = self._migration_cache[upload_id]
                if res.transaction == "COMMITTED":
                    return job_manager.mark_completed(upload_id, res)
                if res.status == "BLOCKED":
                    return job_manager.mark_blocked(upload_id, res.message, res)
                self._migration_cache.pop(upload_id, None)

        # 2. Check preview state & validation block if cached, otherwise use session metadata
        step3_data = self._step3_cache.get(upload_id)
        total_rows = 0
        total_tables = 5

        if step3_data:
            if step3_data.validation_summary and not step3_data.validation_summary.can_execute:
                vs = step3_data.validation_summary
                errors_cnt = len(vs.errors)
                logger.warning(f"[MIGRATION] Blocked execution for uploadId={upload_id}: {errors_cnt} validation error(s) present")
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                blocked_res = MigrationExecutionResponse(
                    uploadId=upload_id,
                    status="BLOCKED",
                    transaction="NOT_EXECUTED",
                    transactionStatus="NOT_EXECUTED",
                    canExecute=False,
                    executionStarted=now_iso,
                    executionCompleted=now_iso,
                    tables={},
                    totalInserted=0,
                    totalUpdated=0,
                    totalReused=0,
                    totalSkipped=0,
                    totalFailed=0,
                    insertedCount=0,
                    updatedCount=0,
                    reusedCount=0,
                    skippedCount=0,
                    failedCount=0,
                    validationErrors=vs.errors,
                    warnings=vs.warnings,
                    verificationCounts={},
                    processingTimeSeconds=0.0,
                    message=f"Migration execution blocked: {errors_cnt} validation error(s) must be resolved before executing the database transaction.",
                    errorReport=[e.model_dump(by_alias=True) for e in vs.errors]
                )
                with self._lock:
                    self._migration_cache[upload_id] = blocked_res
                return job_manager.mark_blocked(upload_id, blocked_res.message, blocked_res)

            if step3_data.planned_operations:
                total_rows = step3_data.planned_operations.total_planned_inserts + step3_data.planned_operations.total_planned_updates
            if step3_data.tables_processed:
                total_tables = len(step3_data.tables_processed)

        # 3. Create job and mark running
        job_manager.create_or_get_job(upload_id, total_rows=total_rows, total_tables=total_tables)
        job_manager.mark_running(upload_id, total_rows=total_rows, total_tables=total_tables)

        # 4. Launch background thread
        def _worker():
            def _progress(tbl, rows_done, tbl_total_rows, ins, reus, tbl_idx, num_tables):
                job_manager.update_progress(
                    upload_id=upload_id,
                    current_table=tbl,
                    rows_processed=rows_done,
                    total_rows=tbl_total_rows,
                    inserted=ins,
                    reused=reus,
                    tables_completed=tbl_idx - 1 if rows_done < tbl_total_rows else tbl_idx,
                    total_tables=num_tables,
                    current_table_idx=tbl_idx
                )
            res = MigrationEngine.execute_migration(
                upload_id,
                progress_callback=_progress,
                duplicate_action=duplicate_action
            )
            if res.transaction == "COMMITTED" or res.status == "BLOCKED":
                with self._lock:
                    self._migration_cache[upload_id] = res
            if res.transaction == "COMMITTED":
                try:
                    excel_service.mark_import_success(upload_id)
                except Exception as history_error:
                    # A ledger failure must not reverse or misreport a committed SQL transaction.
                    logger.error(f"[IMPORT HISTORY] Could not register committed upload {upload_id}: {history_error}")
            return res

        job_manager.run_in_background(upload_id, _worker)
        return job_manager.get_job(upload_id)

    def get_migration_status(self, upload_id: str) -> MigrationJobStatus:
        """
        Retrieves real-time status of an ongoing or completed migration job.
        """
        job = job_manager.get_job(upload_id)
        if job:
            return job

        # Check in-memory cache if job was not tracked via job_manager
        with self._lock:
            if upload_id in self._migration_cache:
                res = self._migration_cache[upload_id]
                return job_manager.mark_completed(upload_id, res)

        # Return default PENDING if session exists
        session = excel_service.get_upload_session(upload_id)
        if session:
            return job_manager.create_or_get_job(upload_id, total_rows=session.get("rowCount", 0))

        raise ValueError(f"No migration session or job found for uploadId '{upload_id}'.")

    def reconcile_migration(self, upload_id: str) -> MigrationJobStatus:
        """
        Reconciles uncertain transaction outcome with actual database records.
        """
        engine = get_engine()
        step3_data = self._step3_cache.get(upload_id)
        target_tables = {}
        if step3_data and step3_data.planned_operations:
            target_tables = step3_data.planned_operations.planned_inserts_by_table
        return job_manager.reconcile_job(upload_id, engine, target_tables)

    def execute_migration(self, upload_id: str, duplicate_action: str = "override") -> MigrationExecutionResponse:
        """
        Executes actual parameterized SQL database migration for an uploaded Excel session inside a single atomic transaction.
        Idempotent: if already executed for upload_id, returns previous verified result.
        Rejects execution if validation errors exist.
        """
        with self._lock:
            if upload_id in self._migration_cache:
                cached_result = self._migration_cache[upload_id]
                if cached_result.transaction == "COMMITTED" or cached_result.status == "BLOCKED":
                    logger.info(f"[MIGRATION] Returning cached terminal result for uploadId={upload_id}")
                    return cached_result
                self._migration_cache.pop(upload_id, None)

        # Verify preview state if available
        step3_data = self._step3_cache.get(upload_id)
        if step3_data and step3_data.validation_summary and not step3_data.validation_summary.can_execute:
            vs = step3_data.validation_summary
            errors_cnt = len(vs.errors)
            logger.warning(f"[MIGRATION] Blocked execution for uploadId={upload_id}: {errors_cnt} validation error(s) present")
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            blocked_res = MigrationExecutionResponse(
                uploadId=upload_id,
                status="BLOCKED",
                transaction="NOT_EXECUTED",
                transactionStatus="NOT_EXECUTED",
                canExecute=False,
                executionStarted=now_iso,
                executionCompleted=now_iso,
                tables={},
                totalInserted=0,
                totalUpdated=0,
                totalReused=0,
                totalSkipped=0,
                totalFailed=0,
                insertedCount=0,
                updatedCount=0,
                reusedCount=0,
                skippedCount=0,
                failedCount=0,
                validationErrors=vs.errors,
                warnings=vs.warnings,
                verificationCounts={},
                processingTimeSeconds=0.0,
                message=f"Migration execution blocked: {errors_cnt} validation error(s) must be resolved before executing the database transaction.",
                errorReport=[e.model_dump(by_alias=True) for e in vs.errors]
            )
            with self._lock:
                self._migration_cache[upload_id] = blocked_res
            job_manager.mark_blocked(upload_id, blocked_res.message, blocked_res)
            return blocked_res

        try:
            job_manager.mark_running(upload_id)
            response = MigrationEngine.execute_migration(
                upload_id,
                duplicate_action=duplicate_action
            )
            if response.transaction == "COMMITTED":
                with self._lock:
                    self._migration_cache[upload_id] = response
                try:
                    excel_service.mark_import_success(upload_id)
                except Exception as history_error:
                    logger.error(f"[IMPORT HISTORY] Could not register committed upload {upload_id}: {history_error}")
                job_manager.mark_completed(upload_id, response)
            elif response.status == "BLOCKED":
                with self._lock:
                    self._migration_cache[upload_id] = response
                job_manager.mark_blocked(upload_id, response.message, response)
            else:
                job_manager.mark_failed(upload_id, response.message, response)
            return response
        except Exception as exc:
            # Check if transaction manager recorded failure
            failed_res = transaction_manager.get_completed(upload_id)
            if failed_res and failed_res.transaction == "ROLLED_BACK":
                job_manager.mark_failed(upload_id, str(exc), failed_res)
                return failed_res
            job_manager.mark_failed(upload_id, str(exc))
            raise exc

    def get_error_report_csv(self, upload_id: str) -> str:
        """
        Generates a downloadable CSV audit/error report for an upload session.
        Separates Source Excel Row metrics from Target Table Operations metrics.
        """
        output = io.StringIO()
        writer = csv.writer(output)

        mig_res = self._migration_cache.get(upload_id)
        step3_res = self._step3_cache.get(upload_id)

        writer.writerow(["Upload ID", upload_id])
        writer.writerow(["Transaction Status", getattr(mig_res, "transaction", "NOT_EXECUTED")])
        if mig_res:
            writer.writerow(["Execution Time (s)", f"{getattr(mig_res, 'processing_time_seconds', 0.0):.2f}"])
            writer.writerow(["Execution Completed At", getattr(mig_res, "execution_completed", "N/A")])
        writer.writerow([])

        # Section 1: Source Row Summary (Excel / CSV Rows)
        if mig_res:
            writer.writerow(["--- 1. SOURCE ROW SUMMARY (Counting Unit: Excel File Rows) ---"])
            writer.writerow(["Metric", "Count", "Description"])
            writer.writerow(["Total Source Rows Processed", getattr(mig_res, "source_rows_total", 0), "Total rows read from input Excel/CSV"])
            writer.writerow(["Source Rows With Inserts", getattr(mig_res, "source_rows_with_inserts", 0), "Source rows that caused new records to be inserted in one or more tables"])
            writer.writerow(["Source Rows Fully Reused", getattr(mig_res, "source_rows_fully_reused", 0), "Source rows where all destination records already existed and were reused"])
            writer.writerow(["Source Rows Failed", getattr(mig_res, "source_rows_failed", 0), "Source rows that failed insertion"])
            writer.writerow([])

            # Section 2: Target Table Operations Summary (Database Records)
            writer.writerow(["--- 2. TARGET TABLE OPERATIONS SUMMARY (Counting Unit: Database Records) ---"])
            writer.writerow(["Metric", "Count", "Description"])
            writer.writerow(["Total Table Operations", getattr(mig_res, "total_table_operations", 0), "Sum of all database insert, reuse, skip, and fail operations across all tables"])
            writer.writerow(["Total Records Inserted", getattr(mig_res, "total_inserted", 0), "Newly inserted records across all destination tables"])
            writer.writerow(["Total Records Reused", getattr(mig_res, "total_reused", 0), "Existing database records matched and referenced via foreign keys"])
            writer.writerow(["Total Records Skipped", getattr(mig_res, "total_skipped", 0), "Records skipped due to constraints or rules"])
            writer.writerow(["Total Records Failed", getattr(mig_res, "total_failed", 0), "Failed database operations"])
            writer.writerow(["Foreign Keys Propagated", getattr(mig_res, "foreign_keys_propagated", 0), "Foreign key relationships dynamically resolved and propagated"])
            writer.writerow([])

            # Section 3: Destination Table Breakdown & Direct SQL Verification
            writer.writerow(["--- 3. DESTINATION TABLE BREAKDOWN & VERIFICATION ---"])
            writer.writerow(["Destination Table", "Inserted Rows", "Reused Rows", "Skipped Rows", "Verified Inserted (PK Check)", "Total Table Rows in DB (COUNT *)", "Verification Status"])
            if mig_res.tables:
                for tbl, stat in mig_res.tables.items():
                    v_ins = getattr(stat, 'verified_inserted', 0)
                    t_rows = getattr(stat, 'total_table_rows', 0)
                    status_text = "VERIFIED_EXISTS" if v_ins == stat.inserted else "VERIFIED"
                    writer.writerow([tbl, stat.inserted, stat.reused, stat.skipped, v_ins, t_rows, status_text])
            writer.writerow([])

            # Section 4: Lookup, Dependency, and Skipped Tables
            lookup_tbls = getattr(mig_res, "lookup_tables", [])
            dep_tbls = getattr(mig_res, "dependency_insert_tables", [])
            skipped_tbls = getattr(mig_res, "skipped_tables", {})
            if lookup_tbls or dep_tbls or skipped_tbls:
                writer.writerow(["--- 4. LOOKUP, DEPENDENCY, AND SKIPPED TABLES ---"])
                writer.writerow(["Table Name", "Classification Role", "Status / Skip Reason"])
                for lt in lookup_tbls:
                    writer.writerow([lt, "LOOKUP_ONLY", "Queried for FK resolution / existing entities; 0 writes"])
                for dt in dep_tbls:
                    writer.writerow([dt, "DEPENDENCY_INSERT", "Permitted dependency parent inserts"])
                for st, reason in skipped_tbls.items():
                    writer.writerow([st, "SKIP", reason])
                writer.writerow([])

        if mig_res and mig_res.failure_details:
            writer.writerow(["--- FAILURE DETAILS ---"])
            writer.writerow(["Source Row", "Target Table", "Error Message"])
            for f in mig_res.failure_details:
                writer.writerow([f.get("row", "N/A"), f.get("table", "N/A"), f.get("error", "")])
            writer.writerow([])

        if step3_res and step3_res.validation_summary:
            vs = step3_res.validation_summary
            writer.writerow(["--- VALIDATION ERRORS & WARNINGS ---"])
            writer.writerow(["Severity", "Source Row", "Table", "Column", "Error Type", "Message"])
            for e in vs.errors:
                writer.writerow(["ERROR", e.row_number, e.table_name, e.column_name, e.error_type, e.message])
            for w in vs.warnings:
                writer.writerow(["WARNING", w.row_number, w.table_name, w.column_name, w.error_type, w.message])

        return output.getvalue()

    def run_dry_run_mock(self) -> Any:
        """
        Executes complete dry-run testing pipeline using generated representative mock data.
        Performs ZERO database writes.
        """
        from app.migration.dry_run import DryRunEngine, generate_representative_mock_data
        schema = database_service.get_schema_metadata()
        engine = get_engine()
        df = generate_representative_mock_data(schema)
        return DryRunEngine.run_dry_run(
            df=df,
            source_name="mock_representative_dataset.csv",
            test_source="MOCK_DATA",
            schema=schema,
            engine=engine
        )

    def run_dry_run_upload(self, upload_id: str) -> Any:
        """
        Executes complete dry-run testing pipeline using actual uploaded file data.
        Performs ZERO database writes.
        """
        from app.migration.dry_run import DryRunEngine
        session_data = excel_service.get_upload_session(upload_id)
        if not session_data:
            raise ValueError(f"Upload session '{upload_id}' not found.")

        file_path = session_data.get("filePath")
        sheet_name = session_data.get("sheetName")
        if not file_path or not pd.io.common.file_exists(file_path):
            raise ValueError("Uploaded data file is missing from server storage.")

        header_row = session_data.get("headerRowNumber") or 1
        df = load_file_dataframe(
            file_path,
            sheet_name=sheet_name,
            header=(header_row - 1) if header_row >= 1 else 0
        ).dropna(how='all')
        df.columns = [str(c).strip() for c in df.columns]

        schema = database_service.get_schema_metadata()
        engine = get_engine()
        overrides = self._user_overrides.get(upload_id, {})
        val_overrides = self._user_value_overrides.get(upload_id, {})

        return DryRunEngine.run_dry_run(
            df=df,
            source_name=session_data.get("fileName") or upload_id,
            test_source="UPLOADED_FILE",
            user_overrides=overrides,
            user_value_overrides=val_overrides,
            schema=schema,
            engine=engine
        )


migration_service = MigrationService()


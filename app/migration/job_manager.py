import os
import json
import time
import datetime
import threading
from typing import Dict, Any, Optional, Callable
from sqlalchemy import text, Engine

from app.models.migration import MigrationJobStatus, MigrationExecutionResponse
from app.models.schema import DatabaseSchemaMetadata
from app.core.logging import logger

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "jobs")
os.makedirs(DATA_DIR, exist_ok=True)


class MigrationJobManager:
    """
    Manages asynchronous migration execution tracking, progress updates, disk persistence,
    and reconciliation across backend restarts.
    """

    def __init__(self, storage_dir: str = DATA_DIR):
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self._jobs: Dict[str, MigrationJobStatus] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._start_times: Dict[str, float] = {}
        self._last_disk_persists: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _get_job_file_path(self, upload_id: str) -> str:
        safe_id = "".join(c for c in upload_id if c.isalnum() or c in ("-", "_"))
        return os.path.join(self.storage_dir, f"{safe_id}.json")

    def _persist_to_disk(self, job: MigrationJobStatus) -> None:
        """Persists job status to disk in atomic manner."""
        try:
            file_path = self._get_job_file_path(job.upload_id)
            tmp_path = f"{file_path}.tmp"
            data = job.model_dump(by_alias=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            if os.path.exists(file_path):
                os.replace(tmp_path, file_path)
            else:
                os.rename(tmp_path, file_path)
        except Exception as e:
            logger.warning(f"[JOB_MANAGER] Failed to persist job {job.upload_id} to disk: {e}")

    def _load_from_disk(self, upload_id: str) -> Optional[MigrationJobStatus]:
        file_path = self._get_job_file_path(upload_id)
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            job = MigrationJobStatus(**data)
            # If loaded from disk and marked RUNNING, but no active thread is running,
            # it means the server restarted while the job was in flight
            if job.status == "RUNNING" and upload_id not in self._threads:
                job.transaction_status = "UNCERTAIN"
                job.message = "Server restarted while migration was in progress. State reconciliation required."
            return job
        except Exception as e:
            logger.warning(f"[JOB_MANAGER] Failed to load job {upload_id} from disk: {e}")
            return None

    def get_job(self, upload_id: str) -> Optional[MigrationJobStatus]:
        with self._lock:
            if upload_id in self._jobs:
                return self._jobs[upload_id]
        # Check disk
        job = self._load_from_disk(upload_id)
        if job:
            with self._lock:
                self._jobs[upload_id] = job
        return job

    def create_or_get_job(
        self,
        upload_id: str,
        total_rows: int = 0,
        total_tables: int = 0
    ) -> MigrationJobStatus:
        with self._lock:
            existing = self._jobs.get(upload_id)
            if not existing:
                disk_job = self._load_from_disk(upload_id)
                if disk_job:
                    self._jobs[upload_id] = disk_job
                    existing = disk_job

            if existing:
                # If currently running or already completed, return existing job
                if existing.status in ("RUNNING", "COMPLETED", "BLOCKED"):
                    return existing

            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            f_id = None
            try:
                from app.services.excel_service import excel_service
                s_data = excel_service.get_upload_session(upload_id) or {}
                f_id = s_data.get("fileId")
            except Exception:
                pass

            job = MigrationJobStatus(
                jobId=f"job_{upload_id}",
                uploadId=upload_id,
                fileId=f_id,
                status="PENDING",
                transactionStatus="NOT_EXECUTED",
                totalRows=total_rows,
                totalTables=total_tables,
                startedAt=now_iso,
                message="Migration job queued and preparing for execution."
            )
            self._jobs[upload_id] = job
            self._start_times[upload_id] = time.time()
            self._persist_to_disk(job)
            return job

    def mark_running(self, upload_id: str, total_rows: int = 0, total_tables: int = 0) -> MigrationJobStatus:
        with self._lock:
            job = self._jobs.get(upload_id) or self._load_from_disk(upload_id)
            if not job:
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                job = MigrationJobStatus(
                    jobId=f"job_{upload_id}",
                    uploadId=upload_id,
                    status="RUNNING",
                    transactionStatus="IN_PROGRESS",
                    totalRows=total_rows,
                    totalTables=total_tables,
                    startedAt=now_iso,
                    message="Executing atomic database transaction..."
                )
            else:
                job.status = "RUNNING"
                job.transaction_status = "IN_PROGRESS"
                if total_rows > 0:
                    job.total_rows = total_rows
                if total_tables > 0:
                    job.total_tables = total_tables
                job.message = "Executing atomic database transaction..."

            self._jobs[upload_id] = job
            if upload_id not in self._start_times:
                self._start_times[upload_id] = time.time()
            self._persist_to_disk(job)
            return job

    def update_progress(
        self,
        upload_id: str,
        current_table: str,
        rows_processed: int,
        total_rows: int,
        inserted: int,
        reused: int,
        tables_completed: int = 0,
        total_tables: int = 0,
        current_table_idx: Optional[int] = None
    ) -> None:
        with self._lock:
            job = self._jobs.get(upload_id)
            if not job:
                return

            t_start = self._start_times.get(upload_id, time.time())
            elapsed = round(time.time() - t_start, 2)

            pct = 0.0
            num_tables = total_tables if total_tables > 0 else job.total_tables
            active_tbl_idx = current_table_idx
            if active_tbl_idx is None and num_tables > 1 and tables_completed > 0:
                active_tbl_idx = min(num_tables, tables_completed + 1)

            if active_tbl_idx is not None and num_tables > 1:
                # Monotonic global progress across all migration tables
                idx_zero = max(0, min(num_tables - 1, active_tbl_idx - 1))
                base_pct = (idx_zero / num_tables) * 100.0
                slice_pct = 100.0 / num_tables
                table_ratio = (rows_processed / total_rows) if total_rows > 0 else 0.0
                table_ratio = min(1.0, max(0.0, table_ratio))
                pct = round(min(99.0, base_pct + (slice_pct * table_ratio)), 1)
            elif total_rows > 0:
                pct = round(min(99.0, (rows_processed / total_rows) * 100.0), 1)

            job.current_table = current_table
            job.rows_processed = rows_processed
            job.total_rows = max(job.total_rows, total_rows)
            job.total_inserted = inserted
            job.total_reused = reused
            job.tables_completed = tables_completed
            if total_tables > 0:
                job.total_tables = total_tables
            # Strictly monotonic progress across migration: never resets or decreases
            job.percent_complete = max(job.percent_complete, pct)
            job.elapsed_seconds = elapsed
            job.message = "Processing and saving Excel data..."
            now_ts = time.time()
            if now_ts - self._last_disk_persists.get(upload_id, 0.0) >= 1.0 or rows_processed >= total_rows:
                self._persist_to_disk(job)
                self._last_disk_persists[upload_id] = now_ts

    def mark_completed(self, upload_id: str, result: MigrationExecutionResponse) -> MigrationJobStatus:
        with self._lock:
            job = self._jobs.get(upload_id) or self._load_from_disk(upload_id)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            t_start = self._start_times.get(upload_id, time.time())
            elapsed = round(time.time() - t_start, 2)

            if not job:
                job = MigrationJobStatus(
                    jobId=f"job_{upload_id}",
                    uploadId=upload_id,
                    status="COMPLETED",
                    transactionStatus="COMMITTED",
                    startedAt=now_iso,
                    completedAt=now_iso,
                    percentComplete=100.0,
                    elapsedSeconds=elapsed,
                    sourceRowsTotal=result.source_rows_total,
                    sourceRowsWithInserts=result.source_rows_with_inserts,
                    sourceRowsFullyReused=result.source_rows_fully_reused,
                    totalTableOperations=result.total_table_operations,
                    totalInserted=result.total_inserted,
                    totalReused=result.total_reused,
                    result=result,
                    message="Database migration completed and transaction committed successfully."
                )
            else:
                job.status = "COMPLETED"
                job.transaction_status = "COMMITTED"
                job.completed_at = now_iso
                job.percent_complete = 100.0
                job.elapsed_seconds = elapsed
                job.total_inserted = result.total_inserted
                job.total_reused = result.total_reused
                job.total_table_operations = result.total_table_operations
                job.source_rows_total = result.source_rows_total
                job.source_rows_with_inserts = result.source_rows_with_inserts
                job.source_rows_fully_reused = result.source_rows_fully_reused
                job.rows_processed = result.source_rows_total if result.source_rows_total > 0 else (job.total_rows if job.total_rows > 0 else (result.total_inserted + result.total_reused))
                job.result = result
                job.message = "Database migration completed and transaction committed successfully."

            file_id = getattr(result, "file_id", None) or getattr(job, "file_id", None)
            if not file_id:
                try:
                    from app.services.excel_service import excel_service
                    s_data = excel_service.get_upload_session(upload_id) or {}
                    file_id = s_data.get("fileId")
                except Exception:
                    pass
            job.file_id = file_id
            if file_id:
                try:
                    from app.services.file_tracking_service import file_tracking_service
                    file_tracking_service.update_file_status(file_id, "Completed")
                except Exception as ft_err:
                    logger.warning(f"[FILE_TRACKING] Error updating status to Completed: {ft_err}")

            self._jobs[upload_id] = job
            self._threads.pop(upload_id, None)
            self._persist_to_disk(job)
            return job

    def mark_failed(self, upload_id: str, error_message: str, result: Optional[MigrationExecutionResponse] = None) -> MigrationJobStatus:
        with self._lock:
            job = self._jobs.get(upload_id) or self._load_from_disk(upload_id)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            t_start = self._start_times.get(upload_id, time.time())
            elapsed = round(time.time() - t_start, 2)

            if not job:
                job = MigrationJobStatus(
                    jobId=f"job_{upload_id}",
                    uploadId=upload_id,
                    status="FAILED",
                    transactionStatus="ROLLED_BACK",
                    startedAt=now_iso,
                    completedAt=now_iso,
                    elapsedSeconds=elapsed,
                    errorMessage=error_message,
                    result=result,
                    message=f"Migration failed and transaction was rolled back: {error_message}"
                )
            else:
                job.status = "FAILED"
                job.transaction_status = "ROLLED_BACK"
                job.completed_at = now_iso
                job.elapsed_seconds = elapsed
                job.error_message = error_message
                job.result = result
                job.message = f"Migration failed and transaction was rolled back: {error_message}"

            file_id = getattr(result, "file_id", None) if result else None
            if not file_id:
                file_id = getattr(job, "file_id", None)
            if not file_id:
                try:
                    from app.services.excel_service import excel_service
                    s_data = excel_service.get_upload_session(upload_id) or {}
                    file_id = s_data.get("fileId")
                except Exception:
                    pass
            job.file_id = file_id
            if file_id:
                try:
                    from app.services.file_tracking_service import file_tracking_service
                    file_tracking_service.update_file_status(file_id, "Failed")
                except Exception as ft_err:
                    logger.warning(f"[FILE_TRACKING] Error updating status to Failed: {ft_err}")

            self._jobs[upload_id] = job
            self._threads.pop(upload_id, None)
            self._persist_to_disk(job)
            return job

    def mark_blocked(self, upload_id: str, message: str, result: MigrationExecutionResponse) -> MigrationJobStatus:
        with self._lock:
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            job = MigrationJobStatus(
                jobId=f"job_{upload_id}",
                uploadId=upload_id,
                status="BLOCKED",
                transactionStatus="NOT_EXECUTED",
                startedAt=now_iso,
                completedAt=now_iso,
                percentComplete=0.0,
                elapsedSeconds=0.0,
                errorMessage=message,
                result=result,
                message=message
            )
            file_id = getattr(result, "file_id", None) if result else None
            if not file_id:
                try:
                    from app.services.excel_service import excel_service
                    s_data = excel_service.get_upload_session(upload_id) or {}
                    file_id = s_data.get("fileId")
                except Exception:
                    pass
            job.file_id = file_id
            if file_id:
                try:
                    from app.services.file_tracking_service import file_tracking_service
                    file_tracking_service.update_file_status(file_id, "Failed")
                except Exception as ft_err:
                    logger.warning(f"[FILE_TRACKING] Error updating status to Failed: {ft_err}")

            self._jobs[upload_id] = job
            self._threads.pop(upload_id, None)
            self._persist_to_disk(job)
            return job

    def is_running(self, upload_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(upload_id)
            if thread and thread.is_alive():
                return True
            job = self._jobs.get(upload_id)
            return bool(job and job.status == "RUNNING")

    def run_in_background(
        self,
        upload_id: str,
        worker_fn: Callable[[], MigrationExecutionResponse]
    ) -> bool:
        """
        Starts execution in a managed background thread.
        Returns True if newly started, False if already in flight.
        """
        with self._lock:
            if upload_id in self._threads and self._threads[upload_id].is_alive():
                logger.info(f"[JOB_MANAGER] Job for uploadId={upload_id} already running in background.")
                return False

            def _target():
                try:
                    logger.info(f"[JOB_MANAGER] Background worker thread started for uploadId={upload_id}")
                    res = worker_fn()
                    if res.transaction == "COMMITTED" or res.status == "COMPLETED":
                        self.mark_completed(upload_id, res)
                    elif res.transaction == "NOT_EXECUTED" or res.status == "BLOCKED":
                        self.mark_blocked(upload_id, res.message, res)
                    else:
                        self.mark_failed(upload_id, res.message, res)
                except Exception as exc:
                    logger.error(f"[JOB_MANAGER] Background worker encountered unhandled error for {upload_id}: {exc}")
                    self.mark_failed(upload_id, str(exc))

            th = threading.Thread(target=_target, name=f"MigrationWorker-{upload_id}", daemon=True)
            self._threads[upload_id] = th
            th.start()
            return True

    def reconcile_job(
        self,
        upload_id: str,
        engine: Engine,
        target_tables: Optional[Dict[str, int]] = None
    ) -> MigrationJobStatus:
        """
        Reconciles job state with database when outcome is uncertain (e.g. after server restart or timeout).
        Queries SQL Server table counts to verify whether records were actually committed or rolled back.
        """
        job = self.get_job(upload_id)
        if not job:
            raise ValueError(f"No job found for uploadId '{upload_id}' to reconcile.")

        if job.status == "COMPLETED" and job.transaction_status == "COMMITTED":
            return job

        logger.info(f"[JOB_MANAGER] Reconciling state for uploadId={upload_id} with database...")
        try:
            with engine.connect() as conn:
                dialect = engine.dialect.name
                counts: Dict[str, int] = {}
                tables_to_check = list(target_tables.keys()) if target_tables else []

                for tbl in tables_to_check:
                    clean_tbl = tbl.replace("[", "").replace("]", "").replace('"', '')
                    quoted = f"[{clean_tbl}]" if dialect == "mssql" else f'"{clean_tbl}"'
                    cnt = conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar() or 0
                    counts[tbl] = int(cnt)

                # If counts exceed 0 for tables that had planned inserts, transaction committed
                has_rows = any(cnt > 0 for cnt in counts.values())
                if has_rows and job.total_inserted > 0:
                    job.status = "COMPLETED"
                    job.transaction_status = "COMMITTED"
                    job.message = "Transaction status verified: database contains committed records."
                else:
                    job.status = "FAILED"
                    job.transaction_status = "ROLLED_BACK"
                    job.message = "Transaction status verified: no records found, transaction rolled back safely."

                self._persist_to_disk(job)
                return job
        except Exception as e:
            logger.error(f"[JOB_MANAGER] Reconciliation query failed: {e}")
            job.transaction_status = "UNCERTAIN"
            job.message = f"Reconciliation could not query database: {e}"
            self._persist_to_disk(job)
            return job


job_manager = MigrationJobManager()

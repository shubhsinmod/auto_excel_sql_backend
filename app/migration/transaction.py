import time
import datetime
import threading
from typing import Dict, Any, Optional, Callable
from sqlalchemy.engine import Engine, Connection
from app.models.migration import MigrationExecutionResponse, TableMigrationStat
from app.core.logging import logger
from app.core.config import settings


def redact_sensitive_info(message: str) -> str:
    """
    Strips passwords, server connection strings, and credentials from error messages.
    """
    if not message:
        return ""
    clean = message
    if settings.DB_PASSWORD:
        clean = clean.replace(settings.DB_PASSWORD, "[REDACTED]")
    if settings.SECRET_KEY:
        clean = clean.replace(settings.SECRET_KEY, "[REDACTED]")
    return clean


class TransactionManager:
    """
    Manages atomic database migration transactions.
    - Uses engine.begin() to guarantee single-transaction commit or automatic rollback.
    - Prevents concurrent duplicate execution requests for the same uploadId.
    - Idempotent: returns verified previous result on repeat execution calls.
    - Strips credentials from all reported error diagnostics.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # upload_id -> MigrationExecutionResponse
        self._completed_executions: Dict[str, MigrationExecutionResponse] = {}
        # upload_id -> threading.Event (to coordinate duplicate in-flight requests)
        self._in_flight: Dict[str, threading.Event] = {}

    def is_in_flight(self, upload_id: str) -> bool:
        with self._lock:
            return upload_id in self._in_flight

    def get_completed(self, upload_id: str) -> Optional[MigrationExecutionResponse]:
        with self._lock:
            return self._completed_executions.get(upload_id)

    def execute_transactionally(
        self,
        upload_id: str,
        engine: Engine,
        execution_fn: Callable[[Connection], MigrationExecutionResponse]
    ) -> MigrationExecutionResponse:
        """
        Executes database migration logic within an atomic SQLAlchemy engine.begin() context.
        Coordinates concurrent requests so duplicate calls await and share the result.
        """
        # 1. Idempotency Check: if already committed successfully, return cached result
        with self._lock:
            if upload_id in self._completed_executions:
                cached = self._completed_executions[upload_id]
                if cached.transaction == "COMMITTED":
                    logger.info(f"[TRANSACTION] Returning idempotent result for uploadId={upload_id}")
                    return cached

            # 2. Duplicate in-flight check
            if upload_id in self._in_flight:
                event = self._in_flight[upload_id]
                wait_for_completion = True
            else:
                event = threading.Event()
                self._in_flight[upload_id] = event
                wait_for_completion = False

        if wait_for_completion:
            logger.info(f"[TRANSACTION] Duplicate request detected for uploadId={upload_id}. Waiting for active transaction...")
            event.wait(timeout=60.0)
            with self._lock:
                if upload_id in self._completed_executions:
                    return self._completed_executions[upload_id]
                raise RuntimeError("Concurrent transaction did not complete within timeout.")

        # 3. Primary execution thread
        t_start = time.time()
        try:
            # SQL Safety Guard: Ensure dry-run mode never invokes real transaction execution
            if getattr(settings, "DRY_RUN_MODE", False):
                raise RuntimeError(
                    "SAFETY GUARD VIOLATION: Transaction execution attempted while DRY_RUN_MODE is enabled. "
                    "Data-modifying transactions are strictly prohibited in dry-run mode."
                )

            logger.info(f"[TRANSACTION] Beginning atomic transaction context for uploadId={upload_id}")
            
            # engine.begin() creates a transaction context that:
            # - Commits ONLY after the block exits successfully.
            # - Rolls back automatically if any exception is raised.
            with engine.begin() as connection:
                response = execution_fn(connection)

            # Transaction has committed successfully!
            logger.info(f"[TRANSACTION] Transaction committed successfully for uploadId={upload_id}")
            response.transaction = "COMMITTED"
            response.status = "COMPLETED"

            with self._lock:
                self._completed_executions[upload_id] = response
            return response

        except Exception as exc:
            duration = round(time.time() - t_start, 3)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            raw_err = str(exc)
            safe_err = redact_sensitive_info(raw_err)
            logger.error(f"[TRANSACTION] Transaction failed and ROLLED BACK for uploadId={upload_id}: {safe_err}")

            failure_response = MigrationExecutionResponse(
                uploadId=upload_id,
                status="FAILED",
                transaction="ROLLED_BACK",
                transactionStatus="ROLLED_BACK",
                canExecute=False,
                executionStarted=datetime.datetime.fromtimestamp(t_start, tz=datetime.timezone.utc).isoformat(),
                executionCompleted=now_iso,
                tables={},
                totalInserted=0,
                totalUpdated=0,
                totalReused=0,
                totalSkipped=0,
                totalFailed=1,
                insertedCount=0,
                updatedCount=0,
                reusedCount=0,
                skippedCount=0,
                failedCount=1,
                verificationCounts={},
                processingTimeSeconds=duration,
                message=f"Migration failed — all operations in this transaction were rolled back. Error: {safe_err}",
                failureDetails=[{"error": safe_err, "transaction": "ROLLED_BACK"}]
            )

            # Save rolled-back response so callers know transaction rolled back
            with self._lock:
                self._completed_executions[upload_id] = failure_response

            raise RuntimeError(safe_err) from exc

        finally:
            with self._lock:
                if upload_id in self._in_flight:
                    self._in_flight[upload_id].set()
                    del self._in_flight[upload_id]


transaction_manager = TransactionManager()

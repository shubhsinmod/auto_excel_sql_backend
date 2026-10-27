import time
import datetime
import threading
from typing import Dict, Any, Optional, Callable
from sqlalchemy import event
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


class SqlServerTransactionGuard:
    """
    Detects when SQL Server has silently ended the migration transaction.

    Some SQL Server errors (conversion errors, many batch-aborting errors, or any
    error with XACT_ABORT ON) roll back the whole open transaction. The migration
    code catches many statement errors and retries (e.g. multi-row INSERT ->
    row-by-row). After such a rollback the ODBC driver silently starts a NEW
    transaction on the next statement, so:
      * everything written before the error is gone, and
      * everything after it lands in a different transaction.
    The job then still reports success (seen as "Customer verified 0/N",
    "Order verified 10873/11441").

    This guard watches every statement on the migration connection. When a
    statement fails, the next statement first checks @@TRANCOUNT / XACT_STATE();
    if the transaction is gone or doomed it raises with the original error,
    so the run stops and rolls back instead of continuing in a new transaction.
    """

    WRITE_PREFIXES = ("insert", "update", "delete", "merge", "set identity_insert")

    def __init__(self, connection: Connection, upload_id: str):
        self.connection = connection
        self.upload_id = upload_id
        self.writes_seen = False
        self.pending_error: Optional[str] = None
        self.in_flight: Optional[str] = None
        self._checking = False

    def attach(self) -> None:
        event.listen(self.connection, "before_cursor_execute", self._before)
        event.listen(self.connection, "after_cursor_execute", self._after)
        try:
            event.listen(self.connection.engine, "handle_error", self._on_error)
        except Exception:  # pragma: no cover - older SQLAlchemy
            pass

    def detach(self) -> None:
        for target, name, fn in (
            (self.connection, "before_cursor_execute", self._before),
            (self.connection, "after_cursor_execute", self._after),
            (self.connection.engine, "handle_error", self._on_error),
        ):
            try:
                event.remove(target, name, fn)
            except Exception:
                pass

    def _on_error(self, ctx) -> None:
        if ctx.connection is not None and ctx.connection is not self.connection:
            return
        msg = redact_sensitive_info(str(getattr(ctx, "original_exception", "") or ""))
        stmt = (getattr(ctx, "statement", "") or "").strip().replace("\n", " ")[:200]
        self.pending_error = f"{msg} [statement: {stmt}]"
        logger.warning(f"[SQL_ERROR_IN_TRANSACTION] upload_id={self.upload_id} {self.pending_error}")

    TX_STATE_QUERIES = (
        "SELECT @@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID()",
        "SELECT @@TRANCOUNT, XACT_STATE(), (SELECT transaction_id FROM sys.dm_tran_current_transaction)",
        "SELECT @@TRANCOUNT, XACT_STATE(), NULL",
    )

    def _read_state(self, cursor):
        """Return (trancount, xact_state, transaction_id) or None if unreadable."""
        self._checking = True
        try:
            for i, q in enumerate(self.TX_STATE_QUERIES):
                if i < getattr(self, "_query_idx", 0):
                    continue
                try:
                    cursor.execute(q)
                    row = cursor.fetchone()
                    self._query_idx = i
                    return row[0], row[1], row[2]
                except Exception:
                    continue
            return None
        finally:
            self._checking = False

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self._checking:
            return
        if self.in_flight is not None and self.pending_error is None:
            # previous statement never completed -> it raised
            self.pending_error = f"statement failed: {self.in_flight[:200]}"
        if self.pending_error is not None:
            self._verify(cursor)
        elif self.writes_seen and getattr(self, "tx_id", None) is None and not getattr(self, "_tx_id_unavailable", False):
            # Remember which SQL Server transaction holds our writes.
            state = self._read_state(cursor)
            if state is None or state[2] is None:
                self._tx_id_unavailable = True
            else:
                self.tx_id = state[2]
        self.in_flight = statement

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if self._checking:
            return
        self.in_flight = None
        if statement.lstrip().lower().startswith(self.WRITE_PREFIXES):
            self.writes_seen = True

    def _transaction_lost(self, state) -> bool:
        trancount, xact_state, tx_id = state
        if xact_state == -1:
            return True
        if not self.writes_seen:
            return False
        if int(trancount or 0) == 0:
            return True
        known = getattr(self, "tx_id", None)
        # The ODBC driver silently opens a NEW transaction after SQL Server rolls
        # one back, so @@TRANCOUNT alone can look healthy. A changed id cannot.
        return known is not None and tx_id is not None and tx_id != known

    def _verify(self, cursor) -> None:
        err = self.pending_error
        self.pending_error = None
        self.in_flight = None
        state = self._read_state(cursor)
        if state is None:
            logger.warning("[TRANSACTION_GUARD] Could not read transaction state after a SQL error.")
            return
        if self._transaction_lost(state):
            raise RuntimeError(
                "SQL Server rolled back the migration transaction after a database error, so rows "
                "written before it were discarded. The migration was stopped to avoid partial data. "
                f"Original error: {err}"
            )

    def check_before_commit(self) -> None:
        if self.pending_error is None and self.in_flight is not None:
            self.pending_error = f"statement failed: {self.in_flight[:200]}"
        if self.pending_error is None and not self.writes_seen:
            return
        raw = self.connection.connection.cursor()
        try:
            err = self.pending_error or "transaction changed during migration"
            self.pending_error = None
            self.in_flight = None
            state = self._read_state(raw)
            if state is not None and self._transaction_lost(state):
                raise RuntimeError(
                    "SQL Server transaction was rolled back or replaced during the migration; part of the "
                    f"data would have been lost. Migration stopped and rolled back. Last error: {err}"
                )
        finally:
            try:
                raw.close()
            except Exception:
                pass


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
                guard = None
                if getattr(getattr(connection, "dialect", None), "name", "") == "mssql":
                    guard = SqlServerTransactionGuard(connection, upload_id)
                    guard.attach()
                try:
                    response = execution_fn(connection)
                    if guard is not None:
                        guard.check_before_commit()
                finally:
                    if guard is not None:
                        guard.detach()

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

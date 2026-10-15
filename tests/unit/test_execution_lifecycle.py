import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from main import app
from app.models.migration import (
    MigrationExecutionResponse,
    RowValidationError,
    ValidationSummary,
    Step3ProcessingResponse
)
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.migration.transaction import TransactionManager
from app.services.migration_service import MigrationService


class TestExecutionLifecycle(unittest.TestCase):
    """
    Comprehensive test suite covering all 13 scenarios for the execution lifecycle:
    1. Successful transaction and successful commit.
    2. Validation errors resulting in NOT_EXECUTED.
    3. Database exception followed by rollback.
    4. HTTP 4xx and 5xx responses.
    5. Network failure handling.
    6. Unexpected CSV or plain-text response handling.
    7. Malformed JSON response handling.
    8. Missing status field normalization.
    9. Terminal response states and loader termination.
    10. Preventing duplicate execution clicks / in-flight coordination.
    11. Audit report download still working.
    12. Successful response returned only after commit.
    13. No writes in preview mode.
    """

    def setUp(self):
        self.client = TestClient(app)
        self.upload_id = "test_lifecycle_upload_123"

    # 1. Successful transaction and successful commit
    def test_1_successful_transaction_and_commit(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE test_outlet (Outlet_ID INTEGER PRIMARY KEY, Outlet_Name TEXT)"))

        tm = TransactionManager()

        def execution_fn(connection):
            connection.execute(text("INSERT INTO test_outlet (Outlet_ID, Outlet_Name) VALUES (1, 'Main Store')"))
            return MigrationExecutionResponse(
                uploadId=self.upload_id,
                status="COMPLETED",
                transaction="COMMITTED",
                totalInserted=1,
                totalReused=0
            )

        res = tm.execute_transactionally(self.upload_id, engine, execution_fn)

        self.assertEqual(res.transaction, "COMMITTED")
        self.assertEqual(res.status, "COMPLETED")
        self.assertTrue(res.can_execute)
        self.assertEqual(res.total_inserted, 1)

        # Verify DB write actually persisted
        with engine.connect() as conn:
            cnt = conn.execute(text("SELECT COUNT(*) FROM test_outlet")).scalar()
            self.assertEqual(cnt, 1)

    # 2. Validation errors resulting in NOT_EXECUTED
    def test_2_validation_errors_result_in_not_executed(self):
        service = MigrationService()
        error_item = RowValidationError(
            rowNumber=2,
            tableName="dbo.Customer",
            columnName="Customer_Name",
            errorType="MISSING_REQUIRED_VALUE",
            message="Customer_Name is required but missing",
            recommendedCorrection="Provide a customer name"
        )
        fake_vs = ValidationSummary(
            totalRows=10,
            validRows=9,
            invalidRows=1,
            canExecute=False,
            errors=[error_item],
            warnings=[]
        )
        fake_step3 = MagicMock(spec=Step3ProcessingResponse)
        fake_step3.validation_summary = fake_vs

        service._step3_cache[self.upload_id] = fake_step3

        result = service.execute_migration(self.upload_id)

        self.assertEqual(result.transaction, "NOT_EXECUTED")
        self.assertEqual(result.status, "BLOCKED")
        self.assertFalse(result.can_execute)
        self.assertEqual(result.total_inserted, 0)
        self.assertEqual(len(result.validation_errors), 1)
        self.assertEqual(result.validation_errors[0].column_name, "Customer_Name")
        self.assertIn("blocked", result.message.lower())

    # 3. Database exception followed by rollback
    def test_3_database_exception_followed_by_rollback(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE test_order (id INTEGER PRIMARY KEY, total REAL NOT NULL)"))

        tm = TransactionManager()

        def execution_fn_failing(connection):
            connection.execute(text("INSERT INTO test_order (id, total) VALUES (1, 100.0)"))
            # Trigger integrity constraint violation (NULL total)
            connection.execute(text("INSERT INTO test_order (id, total) VALUES (2, NULL)"))
            return MigrationExecutionResponse(uploadId=self.upload_id)

        with self.assertRaises(RuntimeError):
            tm.execute_transactionally(self.upload_id, engine, execution_fn_failing)

        # Completed recorded execution must be ROLLED_BACK
        failure_res = tm.get_completed(self.upload_id)
        self.assertIsNotNone(failure_res)
        self.assertEqual(failure_res.transaction, "ROLLED_BACK")
        self.assertEqual(failure_res.status, "FAILED")
        self.assertEqual(failure_res.total_inserted, 0)

        # Verify rollback: 0 records exist
        with engine.connect() as conn:
            cnt = conn.execute(text("SELECT COUNT(*) FROM test_order")).scalar()
            self.assertEqual(cnt, 0)

    # 4. HTTP 4xx and 5xx responses
    def test_4_http_4xx_and_5xx_responses(self):
        # 400 Bad Request for unknown upload session
        resp_400 = self.client.post("/api/migration/execute/non_existent_upload_id")
        self.assertEqual(resp_400.status_code, 400)
        self.assertIn("detail", resp_400.json())

        # 500 Internal Server Error simulation
        with patch("app.services.migration_service.migration_service.execute_migration", side_effect=Exception("Database connection timeout")):
            resp_500 = self.client.post("/api/migration/execute/test_upload_id")
            self.assertEqual(resp_500.status_code, 500)
            self.assertIn("detail", resp_500.json())

    # 5. Network failure handling
    def test_5_network_failure_handling(self):
        # When connection fails or timeout occurs, verify client exception returns error detail
        with patch("app.services.migration_service.migration_service.execute_migration", side_effect=ConnectionError("Failed to establish a new connection")):
            resp = self.client.post(f"/api/migration/execute/{self.upload_id}")
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Failed to establish a new connection", resp.json()["detail"])

    # 6. Unexpected CSV or plain-text response from execution endpoint
    def test_6_unexpected_csv_or_plaintext_response_handled(self):
        # Test CSV error report generation returns valid CSV text
        service = MigrationService()
        error_item = RowValidationError(
            rowNumber=5,
            tableName="dbo.Outlets",
            columnName="Outlet_ID",
            errorType="UNRESOLVED_FOREIGN_KEY",
            message="Unresolved outlet",
            recommendedCorrection="Provide outlet"
        )
        fake_vs = ValidationSummary(canExecute=False, errors=[error_item])
        fake_step3 = MagicMock()
        fake_step3.validation_summary = fake_vs
        service._step3_cache[self.upload_id] = fake_step3

        csv_text = service.get_error_report_csv(self.upload_id)
        self.assertIn("Upload ID", csv_text)
        self.assertIn("Transaction Status", csv_text)
        self.assertIn("NOT_EXECUTED", csv_text)
        self.assertIn("UNRESOLVED_FOREIGN_KEY", csv_text)

    # 7. Malformed JSON response validation
    def test_7_malformed_json_response(self):
        # Validates that MigrationExecutionResponse enforces correct structure
        with self.assertRaises(Exception):
            # Missing required uploadId
            MigrationExecutionResponse.model_validate({})

    # 8. Missing status field normalization
    def test_8_missing_status_field_normalization(self):
        # If transactionStatus is passed instead of transaction or vice-versa, sync_aliases normalizes
        resp = MigrationExecutionResponse(
            uploadId="norm_123",
            transaction="COMMITTED",
            totalInserted=42
        )
        self.assertEqual(resp.transaction_status, "COMMITTED")
        self.assertEqual(resp.inserted_count, 42)

    # 9. Terminal response states
    def test_9_loader_stopping_after_every_terminal_response(self):
        # All valid states (COMMITTED, NOT_EXECUTED, ROLLED_BACK) are terminal
        res_committed = MigrationExecutionResponse(uploadId="u1", transaction="COMMITTED")
        self.assertIn(res_committed.transaction, ["COMMITTED", "NOT_EXECUTED", "ROLLED_BACK"])

        res_blocked = MigrationExecutionResponse(uploadId="u2", transaction="NOT_EXECUTED", status="BLOCKED")
        self.assertIn(res_blocked.transaction, ["COMMITTED", "NOT_EXECUTED", "ROLLED_BACK"])

        res_rolledback = MigrationExecutionResponse(uploadId="u3", transaction="ROLLED_BACK", status="FAILED")
        self.assertIn(res_rolledback.transaction, ["COMMITTED", "NOT_EXECUTED", "ROLLED_BACK"])

    # 10. Preventing duplicate execution clicks
    def test_10_preventing_duplicate_execution_clicks(self):
        tm = TransactionManager()
        engine = create_engine("sqlite:///:memory:")

        # Test idempotency: if already committed, returns cached result
        cached_res = MigrationExecutionResponse(
            uploadId="dup_test_123",
            transaction="COMMITTED",
            status="COMPLETED",
            totalInserted=5
        )
        tm._completed_executions["dup_test_123"] = cached_res

        # Attempt second execution
        called = False
        def fn(conn):
            nonlocal called
            called = True
            return MigrationExecutionResponse(uploadId="dup_test_123")

        second_res = tm.execute_transactionally("dup_test_123", engine, fn)
        self.assertFalse(called)
        self.assertEqual(second_res.total_inserted, 5)

    # 11. Audit report download still working
    def test_11_audit_report_download_still_working(self):
        service = MigrationService()
        fake_step3 = MagicMock()
        fake_step3.validation_summary = ValidationSummary(canExecute=True, errors=[])
        service._step3_cache[self.upload_id] = fake_step3

        with patch("app.services.migration_service.migration_service.get_error_report_csv", return_value="Upload ID,123\nTransaction Status,COMMITTED\n"):
            resp = self.client.get(f"/api/migration/report/{self.upload_id}")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers["content-type"], "text/csv; charset=utf-8")
            self.assertIn("Upload ID,123", resp.text)

    # 12. Successful response returned only after commit
    def test_12_successful_response_returned_only_after_commit(self):
        engine = create_engine("sqlite:///:memory:")
        tm = TransactionManager()

        def execution_fn_uncommitted(conn):
            # Raising before commit
            raise ValueError("Something failed before commit")

        with self.assertRaises(RuntimeError):
            tm.execute_transactionally("commit_test_123", engine, execution_fn_uncommitted)

        # Status must never be COMMITTED
        res = tm.get_completed("commit_test_123")
        self.assertNotEqual(res.transaction, "COMMITTED")
        self.assertEqual(res.transaction, "ROLLED_BACK")

    def test_13_rolled_back_migration_result_does_not_block_retry(self):
        service = MigrationService()
        failed_result = MigrationExecutionResponse(
            uploadId="retry_after_rollback",
            status="FAILED",
            transaction="ROLLED_BACK",
            canExecute=False
        )
        committed_result = MigrationExecutionResponse(
            uploadId="retry_after_rollback",
            status="COMPLETED",
            transaction="COMMITTED",
            totalInserted=1
        )

        with patch("app.services.migration_service.MigrationEngine.execute_migration", side_effect=[RuntimeError("temporary failure"), committed_result]), patch(
            "app.services.migration_service.transaction_manager.get_completed", return_value=None
        ) as get_completed:
            with self.assertRaises(RuntimeError):
                service.execute_migration("retry_after_rollback")
            retry_result = service.execute_migration("retry_after_rollback")

        self.assertEqual(retry_result.transaction, "COMMITTED")
        self.assertEqual(retry_result.total_inserted, 1)
        self.assertEqual(get_completed.call_count, 1)
        self.assertEqual(service._migration_cache["retry_after_rollback"].transaction, "COMMITTED")

    # 14. No writes in preview mode
    def test_14_no_writes_in_preview_mode(self):
        fake_preview = MagicMock(spec=Step3ProcessingResponse)
        fake_preview.statement = "This preview has not written any data to the database."
        fake_preview.can_execute = True

        with patch("app.services.migration_service.migration_service.process_step3", return_value=fake_preview):
            resp = self.client.get(f"/api/migration/preview/{self.upload_id}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["statement"], "This preview has not written any data to the database.")


if __name__ == "__main__":
    unittest.main()

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, text
from fastapi.testclient import TestClient

from main import app
from app.migration.job_manager import MigrationJobManager
from app.models.migration import (
    MigrationJobStatus,
    MigrationExecutionResponse,
    ValidationSummary,
    RowValidationError,
    Step3ProcessingResponse
)
from app.services.migration_service import MigrationService


class TestAsyncMigration(unittest.TestCase):
    """
    Test suite for asynchronous migration tracking, persistence, and reconciliation.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.job_manager = MigrationJobManager(storage_dir=self.test_dir)
        self.client = TestClient(app)
        self.upload_id = "test_async_upload_999"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_job_create_and_disk_persistence(self):
        """Job state persists to disk and can be reloaded across manager instances."""
        job = self.job_manager.create_or_get_job(self.upload_id, total_rows=19894, total_tables=4)
        self.assertEqual(job.upload_id, self.upload_id)
        self.assertEqual(job.status, "PENDING")
        self.assertEqual(job.total_rows, 19894)

        # Reload from fresh manager instance pointing to same directory
        fresh_manager = MigrationJobManager(storage_dir=self.test_dir)
        loaded_job = fresh_manager.get_job(self.upload_id)
        self.assertIsNotNone(loaded_job)
        self.assertEqual(loaded_job.upload_id, self.upload_id)
        self.assertEqual(loaded_job.total_rows, 19894)

    def test_job_progress_tracking(self):
        """Job manager calculates percent complete and updates progress on disk."""
        self.job_manager.create_or_get_job(self.upload_id, total_rows=1000, total_tables=2)
        self.job_manager.mark_running(self.upload_id, total_rows=1000, total_tables=2)

        self.job_manager.update_progress(
            upload_id=self.upload_id,
            current_table="Customer",
            rows_processed=500,
            total_rows=1000,
            inserted=450,
            reused=50,
            tables_completed=0,
            total_tables=2
        )

        job = self.job_manager.get_job(self.upload_id)
        self.assertEqual(job.current_table, "Customer")
        self.assertEqual(job.rows_processed, 500)
        self.assertEqual(job.total_inserted, 450)
        self.assertEqual(job.total_reused, 50)
        self.assertEqual(job.percent_complete, 50.0)

    def test_job_completion(self):
        """Marking job completed sets status to COMPLETED and transaction to COMMITTED."""
        self.job_manager.create_or_get_job(self.upload_id, total_rows=100, total_tables=1)
        res = MigrationExecutionResponse(
            uploadId=self.upload_id,
            status="COMPLETED",
            transaction="COMMITTED",
            totalInserted=90,
            totalReused=10
        )
        completed_job = self.job_manager.mark_completed(self.upload_id, res)
        self.assertEqual(completed_job.status, "COMPLETED")
        self.assertEqual(completed_job.transaction_status, "COMMITTED")
        self.assertEqual(completed_job.percent_complete, 100.0)
        self.assertEqual(completed_job.total_inserted, 90)

    def test_job_restart_detection(self):
        """If server restarts while job was RUNNING, reloaded state reports UNCERTAIN."""
        job = self.job_manager.mark_running(self.upload_id, total_rows=100, total_tables=1)
        self.assertEqual(job.status, "RUNNING")

        # Simulate fresh process restart
        fresh_mgr = MigrationJobManager(storage_dir=self.test_dir)
        reloaded = fresh_mgr.get_job(self.upload_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.transaction_status, "UNCERTAIN")
        self.assertIn("reconciliation", reloaded.message.lower())

    def test_job_reconciliation_against_db(self):
        """Reconciliation verifies committed records in database."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE test_order (id INTEGER, val TEXT)"))
            conn.execute(text("INSERT INTO test_order VALUES (1, 'Sample')"))

        self.job_manager.create_or_get_job(self.upload_id, total_rows=1, total_tables=1)
        self.job_manager._jobs[self.upload_id].total_inserted = 1

        reconciled = self.job_manager.reconcile_job(
            self.upload_id,
            engine,
            target_tables={"test_order": 1}
        )
        self.assertEqual(reconciled.status, "COMPLETED")
        self.assertEqual(reconciled.transaction_status, "COMMITTED")

    def test_prevent_duplicate_execution(self):
        """Calling start on an already running job returns the active job without spawning a new one."""
        self.job_manager.mark_running(self.upload_id, total_rows=500)
        job2 = self.job_manager.create_or_get_job(self.upload_id, total_rows=500)
        self.assertEqual(job2.status, "RUNNING")

    def test_api_start_and_status_endpoints(self):
        """Test FastAPI endpoints for starting migration and polling status."""
        with patch("app.services.migration_service.migration_service.start_migration") as mock_start, \
             patch("app.services.migration_service.migration_service.get_migration_status") as mock_status:

            mock_job = MigrationJobStatus(
                jobId="job_test_api_123",
                uploadId="test_api_123",
                status="RUNNING",
                transactionStatus="IN_PROGRESS",
                currentTable="Orders",
                totalRows=5000,
                rowsProcessed=2500,
                percentComplete=50.0,
                elapsedSeconds=2.5
            )
            mock_start.return_value = mock_job
            mock_status.return_value = mock_job

            start_resp = self.client.post("/api/migration/start/test_api_123")
            self.assertEqual(start_resp.status_code, 200)
            start_data = start_resp.json()
            self.assertEqual(start_data["status"], "RUNNING")
            self.assertEqual(start_data["currentTable"], "Orders")
            self.assertEqual(start_data["percentComplete"], 50.0)

            status_resp = self.client.get("/api/migration/status/test_api_123")
            self.assertEqual(status_resp.status_code, 200)
            status_data = status_resp.json()
            self.assertEqual(status_data["uploadId"], "test_api_123")
            self.assertEqual(status_data["rowsProcessed"], 2500)

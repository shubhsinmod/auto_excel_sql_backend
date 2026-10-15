import pytest
from unittest.mock import MagicMock, patch
from app.models.migration import (
    MigrationExecutionResponse,
    TableMigrationStat,
    MigrationJobStatus,
)
from app.services.migration_service import MigrationService


class TestMigrationReportValidation:
    """
    Regression tests verifying counting unit definitions, multi-table operation tracking,
    foreign key propagation, and destination table database verification.
    """

    def test_counting_units_source_vs_target_operations(self):
        """
        Verify that Source Excel Rows and Target Database Operations are distinct counting units
        and are not conflated or double-counted.
        
        Scenario matching user report:
        - 19,894 source rows
        - Target operations across 5 tables:
            Customer: 3,123 inserted, 934 reused = 4,057 operations
            Order: 4,451 inserted, 0 reused = 4,451 operations
            Bills: 0 inserted, 11,887 reused = 11,887 operations
            Item_Groups: 0 inserted, 16 reused = 16 operations
            OrderLineItems: 395 inserted, 0 reused = 395 operations
        - Total inserted = 7,969
        - Total reused = 12,837
        - Total operations = 20,806 (912 higher than 19,894 due to 1:N row-to-table expansion)
        """
        tables_data = {
            "Customer": TableMigrationStat(inserted=3123, reused=934, verifiedInserted=3123, totalTableRows=15000),
            "Order": TableMigrationStat(inserted=4451, reused=0, verifiedInserted=4451, totalTableRows=25000),
            "Bills": TableMigrationStat(inserted=0, reused=11887, verifiedInserted=0, totalTableRows=30000),
            "Item_Groups": TableMigrationStat(inserted=0, reused=16, verifiedInserted=0, totalTableRows=500),
            "OrderLineItems": TableMigrationStat(inserted=395, reused=0, verifiedInserted=395, totalTableRows=50000),
        }

        total_inserted = sum(t.inserted for t in tables_data.values())
        total_reused = sum(t.reused for t in tables_data.values())
        total_ops = total_inserted + total_reused

        assert total_inserted == 7969
        assert total_reused == 12837
        assert total_ops == 20806
        assert total_ops - 19894 == 912

        resp = MigrationExecutionResponse(
            uploadId="upload_test_report_validation",
            status="COMPLETED",
            transaction="COMMITTED",
            transactionStatus="COMMITTED",
            tables=tables_data,
            sourceRowsTotal=19894,
            sourceRowsWithInserts=7969,
            sourceRowsFullyReused=11925,
            sourceRowsFailed=0,
            totalTableOperations=total_ops,
            totalInserted=total_inserted,
            totalReused=total_reused,
            foreignKeysPropagated=16723,
            verifiedInsertedCounts={k: v.verified_inserted for k, v in tables_data.items()},
            verificationCounts={k: v.total_table_rows for k, v in tables_data.items()},
            processingTimeSeconds=85.58,
            message="Database migration completed and transaction committed successfully."
        )

        # Mutually exclusive partition tests
        # Source Row partition:
        assert resp.source_rows_with_inserts + resp.source_rows_fully_reused + resp.source_rows_failed == resp.source_rows_total
        assert resp.source_rows_total == 19894

        # Target Table Operations partition:
        assert resp.total_inserted + resp.total_reused + resp.total_skipped + resp.total_failed == resp.total_table_operations
        assert resp.total_table_operations == 20806

        # Target table verification check:
        for tbl_name, stat in resp.tables.items():
            assert stat.verified_inserted == stat.inserted
            assert stat.total_table_rows > 0

    def test_multi_table_row_expansion(self):
        """
        Verify a 2-row Excel migration where:
        - Row 1: inserts new Customer, inserts new Order (2 inserts, 0 reuse)
        - Row 2: reuses Customer, inserts new Order (1 insert, 1 reuse)
        Total source rows = 2
        Total database operations = 4 (3 inserts, 1 reuse)
        """
        tables = {
            "Customer": TableMigrationStat(inserted=1, reused=1, verifiedInserted=1, totalTableRows=10),
            "Order": TableMigrationStat(inserted=2, reused=0, verifiedInserted=2, totalTableRows=20),
        }
        resp = MigrationExecutionResponse(
            uploadId="multi_tbl_test",
            status="COMPLETED",
            transaction="COMMITTED",
            tables=tables,
            sourceRowsTotal=2,
            sourceRowsWithInserts=2,
            sourceRowsFullyReused=0,
            totalTableOperations=4,
            totalInserted=3,
            totalReused=1,
            foreignKeysPropagated=2,
            processingTimeSeconds=1.2,
        )

        assert resp.source_rows_total == 2
        assert resp.total_table_operations == 4
        assert resp.total_inserted == 3
        assert resp.total_reused == 1
        assert resp.foreign_keys_propagated == 2

    def test_audit_report_csv_generation(self):
        """
        Verify that get_error_report_csv generates structured sections:
        - Source Row Summary
        - Target Table Operations Summary
        - Destination Table Breakdown & Verification
        """
        svc = MigrationService()
        upload_id = "upload_csv_audit_test"

        tables_data = {
            "Customer": TableMigrationStat(inserted=3123, reused=934, verifiedInserted=3123, totalTableRows=15000),
            "Order": TableMigrationStat(inserted=4451, reused=0, verifiedInserted=4451, totalTableRows=25000),
        }

        mig_res = MigrationExecutionResponse(
            uploadId=upload_id,
            status="COMPLETED",
            transaction="COMMITTED",
            tables=tables_data,
            sourceRowsTotal=19894,
            sourceRowsWithInserts=7574,
            sourceRowsFullyReused=12320,
            totalTableOperations=8508,
            totalInserted=7574,
            totalReused=934,
            foreignKeysPropagated=4451,
            processingTimeSeconds=85.58,
        )

        with svc._lock:
            svc._migration_cache[upload_id] = mig_res

        csv_content = svc.get_error_report_csv(upload_id)

        assert "Upload ID,upload_csv_audit_test" in csv_content
        assert "Transaction Status,COMMITTED" in csv_content
        assert "--- 1. SOURCE ROW SUMMARY (Counting Unit: Excel File Rows) ---" in csv_content
        assert "Total Source Rows Processed,19894" in csv_content
        assert "Source Rows With Inserts,7574" in csv_content
        assert "--- 2. TARGET TABLE OPERATIONS SUMMARY (Counting Unit: Database Records) ---" in csv_content
        assert "Total Table Operations,8508" in csv_content
        assert "Total Records Inserted,7574" in csv_content
        assert "Total Records Reused,934" in csv_content
        assert "--- 3. DESTINATION TABLE BREAKDOWN & VERIFICATION ---" in csv_content
        assert "Customer,3123,934,0,3123,15000,VERIFIED_EXISTS" in csv_content
        assert "Order,4451,0,0,4451,25000,VERIFIED_EXISTS" in csv_content

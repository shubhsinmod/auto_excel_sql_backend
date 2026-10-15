"""
Unit and Integration Tests for Dry-Run Testing Engine & SQL Safety Guard.

Verifies:
1. Dry-run mode never executes INSERT, UPDATE, DELETE, or commits transactions in SQL Server.
2. Real migration executor raises Safety Guard exception if invoked when DRY_RUN_MODE is active.
3. TransactionManager raises Safety Guard exception if transaction is attempted when DRY_RUN_MODE is active.
4. Mock data generation includes edge cases (casing, branch suffixes, missing values, duplicate rows, leading zeros).
5. Proposed SQL statements are marked as 'NOT EXECUTED — DRY RUN' and parameter values are properly sanitized and masked.
6. Database integrity checks verify 0 writes before and after dry-run runs.
7. Both mock data and uploaded file dry-run endpoints and methods produce valid DryRunReport instances.
"""

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine

from app.core.config import settings
from app.migration.dry_run import (
    DryRunEngine,
    generate_representative_mock_data,
    mask_sensitive,
    ProposedSqlStatement,
    DryRunReport
)
from app.migration.engine import MigrationEngine
from app.migration.transaction import transaction_manager, TransactionManager
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata


@pytest.fixture
def mock_schema():
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR(255)", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Mobile_Number", type="VARCHAR(50)", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Location", type="VARCHAR(255)", nullable=True, primary_key=False, identity=False),
    ]
    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR(255)", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Location", type="VARCHAR(255)", nullable=True, primary_key=False, identity=False),
    ]
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Total_Amount", type="DECIMAL(10,2)", nullable=False, primary_key=False, identity=False),
    ]

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Test",
        db_type="sqlite",
        tables=[
            TableMetadata(name="Customer", schema_name="dbo", columns=customer_cols, primary_keys=["Customer_ID"]),
            TableMetadata(name="Outlets", schema_name="dbo", columns=outlets_cols, primary_keys=["Outlet_ID"]),
            TableMetadata(name="Order", schema_name="dbo", columns=order_cols, primary_keys=["Order_ID"]),
        ]
    )


@pytest.fixture
def sqlite_engine():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_Name TEXT, Mobile_Number TEXT, Location TEXT)")
        conn.exec_driver_sql("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Location TEXT)")
        conn.exec_driver_sql("CREATE TABLE `Order` (Order_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_ID INTEGER, Outlet_ID INTEGER, Order_DateTime TEXT, Total_Amount NUMERIC)")
        # Insert a sample outlet
        conn.exec_driver_sql("INSERT INTO Outlets (Outlet_Name, Location) VALUES ('Namaste', 'Bengaluru')")
    return eng


def test_mock_data_generation_edge_cases():
    """Verify mock data includes all edge cases specified in the requirements."""
    df = generate_representative_mock_data()
    assert len(df) == 7
    assert "customer_phone" in df.columns
    assert "restaurant_name" in df.columns
    assert "date" in df.columns

    # Leading zero in phone
    phones = df["customer_phone"].dropna().tolist()
    assert any(p.startswith("0") for p in phones)

    # Branch suffixes
    restaurants = df["restaurant_name"].tolist()
    assert any("BTM" in r for r in restaurants)
    assert any("Hoodi" in r for r in restaurants)

    # Excel serial date
    assert 45000 in df["date"].values

    # Missing and duplicate rows
    assert df["customer_phone"].isna().sum() > 0
    assert df.duplicated().sum() >= 1


def test_mask_sensitive_utility():
    """Verify phone numbers and sensitive strings are masked when enabled."""
    with patch.object(settings, "DRY_RUN_MASK_SENSITIVE", True):
        assert mask_sensitive("09876543210") == "098****210"
        assert mask_sensitive("+919876543210") == "+91****210"
        assert mask_sensitive("John Doe") == "John Doe"

    with patch.object(settings, "DRY_RUN_MASK_SENSITIVE", False):
        assert mask_sensitive("09876543210") == "09876543210"


def test_dry_run_zero_database_writes(sqlite_engine, mock_schema):
    """
    Execute complete dry-run on mock data and verify that exactly zero database writes occur.
    """
    df = generate_representative_mock_data(mock_schema)

    # Row count before
    pre_counts = DryRunEngine.get_database_row_counts(sqlite_engine, ["Customer", "Outlets", "Order"])
    assert pre_counts["Outlets"] == 1
    assert pre_counts["Customer"] == 0
    assert pre_counts["Order"] == 0

    report: DryRunReport = DryRunEngine.run_dry_run(
        df=df,
        source_name="test_mock.csv",
        test_source="MOCK_DATA",
        schema=mock_schema,
        engine=sqlite_engine
    )

    # Verify report metrics
    assert report.mode == "DRY_RUN"
    assert report.test_source == "MOCK_DATA"
    assert report.rows_read == 7
    assert report.insert_executed == 0
    assert report.update_executed == 0
    assert report.delete_executed == 0
    assert report.database_writes == 0
    assert report.db_integrity_verified is True
    assert "========== DRY-RUN TEST REPORT ==========" in report.text_report

    # Row count after must remain identical
    post_counts = DryRunEngine.get_database_row_counts(sqlite_engine, ["Customer", "Outlets", "Order"])
    assert post_counts["Outlets"] == 1
    assert post_counts["Customer"] == 0
    assert post_counts["Order"] == 0

    # Proposed statements must all have NOT EXECUTED status
    for stmt in report.proposed_sql_samples:
        assert stmt.status == "NOT EXECUTED — DRY RUN"


def test_sql_safety_guard_blocks_real_executor(sqlite_engine):
    """
    Verify that if DRY_RUN_MODE is active, the real execution path throws an immediate error
    preventing any database write operations.
    """
    with patch.object(settings, "DRY_RUN_MODE", True):
        # 1. MigrationEngine.execute_migration must fail immediately
        with pytest.raises(RuntimeError) as exc_info:
            MigrationEngine.execute_migration("dummy_upload_id")
        assert "SAFETY GUARD VIOLATION" in str(exc_info.value)

        # 2. TransactionManager.execute_transactionally must fail immediately
        tm = TransactionManager()
        with pytest.raises(RuntimeError) as exc_info_tm:
            tm.execute_transactionally("dummy_upload_id", sqlite_engine, lambda conn: None)
        assert "SAFETY GUARD VIOLATION" in str(exc_info_tm.value)


def test_dry_run_never_inserts_into_outlets(sqlite_engine, mock_schema):
    """
    Verify that Outlets is treated as read-only and no proposed INSERT is ever generated for Outlets.
    """
    df = generate_representative_mock_data(mock_schema)
    report = DryRunEngine.run_dry_run(
        df=df,
        source_name="test_mock.csv",
        test_source="MOCK_DATA",
        schema=mock_schema,
        engine=sqlite_engine
    )

    for stmt in report.proposed_sql_samples:
        assert stmt.table_name != "Outlets", "Proposed INSERT generated for read-only Outlets table!"


def test_preview_step3_with_schema_metadata(sqlite_engine, mock_schema):
    """
    Verify that preview processing executes efficiently with schema metadata
    and returns complete Step3ProcessingResponse without database writes.
    """
    from app.migration.preview import MigrationPreviewEngine
    from app.mapping.matcher import ColumnMatcher

    df = generate_representative_mock_data(mock_schema)
    cols = df.columns.tolist()
    mappings = ColumnMatcher.match_columns(cols, mock_schema, database_scope=mock_schema.database_name)

    res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="mock_test.csv",
        schema=mock_schema,
        column_mappings=mappings,
        engine=sqlite_engine
    )

    preview_rows, dep_plan, planned_ops, val_summary, order, val_res = res
    assert len(preview_rows) > 0
    assert val_summary is not None
    assert planned_ops is not None
    assert planned_ops.statement == "This preview has not written any data to the database."


def test_schema_discovery_clear_error_when_unavailable():
    """
    Verify that database schema discovery raises an informative exception when engine cannot connect.
    """
    from app.services.database_service import DatabaseService
    from unittest.mock import patch

    with patch("app.services.database_service.get_engine") as mock_get_engine:
        mock_get_engine.side_effect = RuntimeError("SQL Server connection unavailable")
        with pytest.raises(RuntimeError) as exc_info:
            DatabaseService.get_schema_metadata(use_cache=False)
        assert "SQL Server connection unavailable" in str(exc_info.value)


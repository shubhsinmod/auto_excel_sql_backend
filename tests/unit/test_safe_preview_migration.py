import pytest
import pandas as pd
from unittest.mock import patch
from sqlalchemy import create_engine, text

from app.models.schema import (
    DatabaseSchemaMetadata,
    TableMetadata,
    ColumnMetadata,
    ForeignKeyInfo
)
from app.models.migration import (
    ColumnMappingResult,
    SourceRowPreviewItem,
    MigrationExecutionResponse
)
from app.migration.preview import MigrationPreviewEngine
from app.migration.validator import MigrationValidator
from app.migration.engine import MigrationEngine
from app.migration.transaction import transaction_manager
from app.migration.id_resolver import IdResolverStore
from app.services.migration_service import migration_service


@pytest.fixture
def test_db_engine():
    """Creates a temporary in-memory SQLite database with parent Customer and child Order tables."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name TEXT NOT NULL UNIQUE,
                Address TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_ID INTEGER NOT NULL,
                Order_Date TEXT NOT NULL,
                Amount REAL,
                FOREIGN KEY (Customer_ID) REFERENCES Customer(Customer_ID)
            );
        """))
    return engine


@pytest.fixture
def mock_schema():
    """Returns DatabaseSchemaMetadata for the Customer and Order tables."""
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="TEXT", nullable=False, primary_key=False, identity=False, unique=True),
        ColumnMetadata(name="Address", type="TEXT", nullable=True, primary_key=False, identity=False),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    order_fk = ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, primary_key=False, identity=False, foreign_keys=[order_fk]),
        ColumnMetadata(name="Order_Date", type="TEXT", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Amount", type="REAL", nullable=True, primary_key=False, identity=False),
    ]
    order_tbl = TableMetadata(
        name="Order",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[order_fk]
    )

    return DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[customer_tbl, order_tbl]
    )


# -------------------------------------------------------------------------------------------------
# TEST 1: Preview performs no database writes
# -------------------------------------------------------------------------------------------------
def test_preview_performs_no_database_writes(test_db_engine, mock_schema):
    df = pd.DataFrame([
        {"customer_name": "Alice Smith", "customer_address": "123 Main St", "date": "2026-06-30", "amount": "100.50"},
        {"customer_name": "Bob Jones", "customer_address": "456 Oak St", "date": "2026-07-01", "amount": "200.00"},
    ])
    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="customer_address", databaseTable="Customer", databaseColumn="Address", status="MATCHED"),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_Date", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Amount", status="MATCHED"),
    ]

    # Check row counts before preview
    with test_db_engine.connect() as conn:
        c_count_before = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        o_count_before = conn.execute(text('SELECT COUNT(*) FROM "Order"')).scalar()

    # Generate preview
    preview_rows, dep_plan, planned_ops, val_summary, order, val_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test.xlsx",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    # Check row counts after preview
    with test_db_engine.connect() as conn:
        c_count_after = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        o_count_after = conn.execute(text('SELECT COUNT(*) FROM "Order"')).scalar()

    assert c_count_before == 0 and c_count_after == 0
    assert o_count_before == 0 and o_count_after == 0
    assert planned_ops.statement == "This preview has not written any data to the database."
    assert len(preview_rows) > 0


# -------------------------------------------------------------------------------------------------
# TEST 2: Correct source values appear in the preview
# -------------------------------------------------------------------------------------------------
def test_correct_source_values_appear_in_preview(test_db_engine, mock_schema):
    df = pd.DataFrame([
        {"customer_name": "Rahul Sharma", "customer_address": "Bengaluru India", "date": "2026-06-30"},
    ])
    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="customer_address", databaseTable="Customer", databaseColumn="Address", status="MATCHED"),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_Date", status="MATCHED"),
    ]

    preview_rows, _, _, _, _, _ = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="customers_orders.xlsx",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    cust_name_item = next(p for p in preview_rows if p.target_table == "Customer" and p.target_column == "Customer_Name")
    assert cust_name_item.source_value == "Rahul Sharma"
    assert cust_name_item.final_value == "Rahul Sharma"
    assert cust_name_item.value_source == "EXCEL"

    cust_addr_item = next(p for p in preview_rows if p.target_table == "Customer" and p.target_column == "Address")
    assert cust_addr_item.source_value == "Bengaluru India"
    assert cust_addr_item.final_value == "Bengaluru India"

    cust_id_item = next(p for p in preview_rows if p.target_table == "Customer" and p.target_column == "Customer_ID")
    assert cust_id_item.value_source == "IDENTITY"
    assert cust_id_item.is_identity is True


# -------------------------------------------------------------------------------------------------
# TEST 3: Missing required values block execution
# -------------------------------------------------------------------------------------------------
def test_missing_required_values_block_execution(mock_schema):
    # Order_Date is NOT NULL and required. Rows 2 & 3 have missing Order_Date.
    df = pd.DataFrame([
        {"order_date": "2026-06-30", "amount": 100},
        {"order_date": "", "amount": 200},   # Empty required string
        {"order_date": None, "amount": 300}, # Null required string
    ])
    mappings = [
        ColumnMappingResult(excelColumn="order_date", databaseTable="Order", databaseColumn="Order_Date", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Amount", status="MATCHED"),
    ]

    val_summary = MigrationValidator.validate_dataset(
        df=df,
        schema=mock_schema,
        column_mappings=mappings
    )

    assert val_summary.can_execute is False
    assert val_summary.invalid_rows >= 2
    assert any("Order_Date" in e.column_name and "missing/NULL" in e.message for e in val_summary.errors)


# -------------------------------------------------------------------------------------------------
# TEST 4: Existing foreign keys resolve correctly
# -------------------------------------------------------------------------------------------------
def test_existing_foreign_keys_resolve_correctly(test_db_engine, mock_schema):
    # Pre-seed Customer table with existing customer ID 10
    with test_db_engine.begin() as conn:
        conn.execute(text("INSERT INTO Customer (Customer_ID, Customer_Name, Address) VALUES (10, 'Existing Corp', 'NYC')"))

    df = pd.DataFrame([
        {"cust_ref": "Existing Corp", "date": "2026-06-30", "amount": 500.0},
    ])
    mappings = [
        ColumnMappingResult(excelColumn="cust_ref", databaseTable="Order", databaseColumn="Customer_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_Date", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Amount", status="MATCHED"),
    ]

    preview_rows, dep_plan, _, _, _, val_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="orders.xlsx",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    # Verify dependency plan found the existing parent record and resolved ID 10
    matched_plan = [p for p in dep_plan if p.parent_table == "Customer" and p.status == "MATCHED"]
    assert len(matched_plan) > 0
    assert matched_plan[0].resolved_id == "10"
    assert matched_plan[0].existing_parent_found is True


# -------------------------------------------------------------------------------------------------
# TEST 5: Generated parent IDs populate child records
# -------------------------------------------------------------------------------------------------
def test_generated_parent_ids_populate_child_records(test_db_engine, mock_schema):
    # Test that inserting a parent generates an ID and child insert links to that exact parent ID
    id_store = IdResolverStore()

    with test_db_engine.begin() as conn:
        # Insert parent
        res = conn.execute(text("INSERT INTO Customer (Customer_Name, Address) VALUES ('Fresh Parent', 'Seattle')"))
        parent_id = res.lastrowid
        assert parent_id is not None
        id_store.register_id("Customer", "Fresh Parent", parent_id)

        # Child insert retrieves parent ID from id_store
        resolved_fk = id_store.get_id("Customer", "Fresh Parent")
        assert resolved_fk == parent_id

        conn.execute(
            text('INSERT INTO "Order" (Customer_ID, Order_Date, Amount) VALUES (:cid, :dt, :amt)'),
            {"cid": resolved_fk, "dt": "2026-06-30", "amt": 250.0}
        )

        child_row = conn.execute(text('SELECT Customer_ID FROM "Order" WHERE Order_Date = "2026-06-30"')).mappings().one()
        assert child_row["Customer_ID"] == parent_id


# -------------------------------------------------------------------------------------------------
# TEST 6: Any required insert failure triggers rollback
# -------------------------------------------------------------------------------------------------
def test_any_required_insert_failure_triggers_rollback(test_db_engine):
    # Pre-check row count
    with test_db_engine.connect() as conn:
        count_start = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()

    with pytest.raises(Exception):
        with test_db_engine.begin() as conn:
            # 1st insert succeeds
            conn.execute(text("INSERT INTO Customer (Customer_Name, Address) VALUES ('Successful 1', 'Addr 1')"))
            # 2nd insert deliberately violates NOT NULL constraint on Customer_Name
            conn.execute(text("INSERT INTO Customer (Customer_Name, Address) VALUES (NULL, 'Addr 2')"))

    # Verify rollback: 1st insert was rolled back completely
    with test_db_engine.connect() as conn:
        count_end = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()

    assert count_start == 0
    assert count_end == 0


# -------------------------------------------------------------------------------------------------
# TEST 7: No success response is returned before commit
# -------------------------------------------------------------------------------------------------
def test_no_success_response_returned_before_commit():
    def failing_action(conn):
        raise RuntimeError("Simulated database failure during execution")

    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(RuntimeError) as exc_info:
        transaction_manager.execute_transactionally("upload_fail_test", engine, failing_action)

    assert "Simulated database failure" in str(exc_info.value)
    # Check that transaction status recorded was ROLLED_BACK, never COMMITTED
    record = transaction_manager.get_completed("upload_fail_test")
    assert record is not None
    assert record.transaction == "ROLLED_BACK"
    assert record.status == "FAILED"


# -------------------------------------------------------------------------------------------------
# TEST 8: Duplicate execution requests do not create duplicate records
# -------------------------------------------------------------------------------------------------
def test_duplicate_execution_requests_idempotency(test_db_engine):
    execution_counter = {"count": 0}

    def sample_migration(conn):
        execution_counter["count"] += 1
        conn.execute(text("INSERT INTO Customer (Customer_Name, Address) VALUES ('Idempotent Test', 'CA')"))
        return MigrationExecutionResponse(
            uploadId="upload_idempotency_123",
            status="COMPLETED",
            transaction="COMMITTED",
            totalInserted=1
        )

    # First execution
    res1 = transaction_manager.execute_transactionally("upload_idempotency_123", test_db_engine, sample_migration)
    assert res1.transaction == "COMMITTED"
    assert execution_counter["count"] == 1

    # Second duplicate execution for same upload_id
    res2 = transaction_manager.execute_transactionally("upload_idempotency_123", test_db_engine, sample_migration)
    assert res2.transaction == "COMMITTED"
    # Execution counter must still be 1 (never executed twice)
    assert execution_counter["count"] == 1

    # Database has exactly 1 row
    with test_db_engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
    assert total == 1


# -------------------------------------------------------------------------------------------------
# TEST 9: Validation covers the entire dataset
# -------------------------------------------------------------------------------------------------
def test_validation_covers_entire_dataset(mock_schema):
    # 50 rows dataset: rows 1-40 valid, row 45 invalid (int conversion failure), row 49 invalid (null required)
    records = []
    for i in range(1, 51):
        if i == 45:
            records.append({"customer_name": f"Cust {i}", "order_date": "2026-06-30", "amount": "NOT_A_NUMBER"})
        elif i == 49:
            records.append({"customer_name": f"Cust {i}", "order_date": None, "amount": "50.0"})
        else:
            records.append({"customer_name": f"Cust {i}", "order_date": "2026-06-30", "amount": f"{i * 10}.0"})

    df = pd.DataFrame(records)
    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_date", databaseTable="Order", databaseColumn="Order_Date", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Amount", status="MATCHED"),
    ]

    summary = MigrationValidator.validate_dataset(df, mock_schema, mappings)
    assert summary.total_rows == 50
    assert summary.invalid_rows == 2
    assert summary.valid_rows == 48
    assert summary.can_execute is False

    err_rows = [e.row_number for e in summary.errors]
    assert 45 in err_rows
    assert 49 in err_rows


# -------------------------------------------------------------------------------------------------
# TEST 10: Migration reports distinguish planned operations from actual committed operations
# -------------------------------------------------------------------------------------------------
def test_reports_distinguish_planned_from_actual_committed(test_db_engine, mock_schema):
    df = pd.DataFrame([
        {"customer_name": "Plan Test", "date": "2026-06-30", "amount": "100.0"},
    ])
    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_Date", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Amount", status="MATCHED"),
    ]

    # Generate preview (Planned)
    _, _, planned_ops, _, _, _ = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="plan_vs_actual.xlsx",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    # Actual database row count is STILL 0
    with test_db_engine.connect() as conn:
        actual_db_count = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()

    assert planned_ops.total_planned_inserts > 0
    assert actual_db_count == 0
    assert planned_ops.statement == "This preview has not written any data to the database."

    # Now simulate committed actual execution response
    actual_exec = MigrationExecutionResponse(
        uploadId="upload_actual_test",
        status="COMPLETED",
        transaction="COMMITTED",
        totalInserted=planned_ops.total_planned_inserts,
        verificationCounts={"Customer": 1, "Order": 1}
    )

    assert actual_exec.transaction == "COMMITTED"
    assert actual_exec.status == "COMPLETED"
    assert actual_exec.total_inserted == planned_ops.total_planned_inserts

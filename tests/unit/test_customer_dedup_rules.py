"""
Automated tests for Customer Deduplication Rules in Intelligent Excel-to-SQL.

Covers all 15 required test cases:
1. Name Rahul, mobile 9876543210 -> insert one customer.
2. Name Amit, mobile 9876543210 -> NEW customer (name + mobile must both match to reuse).
3. Empty name, mobile 9876543210 -> reuse the existing customer.
4. Existing customer name is 'null', mobile 9876543210; incoming name is Rahul -> new customer, existing row untouched.
5. Name Rahul, empty mobile -> insert a name-only customer.
6. Name Amit, empty mobile -> insert a separate customer.
7. Name Rahul, empty mobile again -> reuse the existing name-only customer.
8. Name Rahul, mobile 9123456780 -> create a separate customer from Rahul with mobile 9876543210.
9. Empty name, new mobile 9123456780 -> insert one customer with Customer_Name = 'null'.
10. Empty name and empty mobile -> skip the row.
11. Mobile values with surrounding spaces or Excel numeric/string representations -> normalize and match consistently.
12. Repeated rows in the same Excel file -> do not create duplicate customers.
13. Existing database customers and incoming Excel customers -> match correctly across multiple migration runs.
14. Related orders -> verify that the correct reused or newly generated Customer_ID is assigned.
15. Simulated database failure -> verify transaction rollback and prevent partial or inconsistent customer/order records.
"""

import pytest
import pandas as pd
from unittest.mock import patch
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult
from app.migration.customer_dedup import (
    normalize_name,
    normalize_mobile,
    is_name_blank_or_null,
    resolve_customer
)
from app.migration.engine import MigrationEngine


# ---------------------------------------------------------------------------
# Basic Normalization Tests
# ---------------------------------------------------------------------------

def test_normalize_mobile_formatting():
    assert normalize_mobile("+91 98765-43210") == "919876543210"
    assert normalize_mobile("9876543210") == "9876543210"
    assert normalize_mobile(9876543210.0) == "9876543210"
    assert normalize_mobile("9876543210.0") == "9876543210"
    assert normalize_mobile("  9876543210  ") == "9876543210"


def test_normalize_name_whitespace_and_case():
    assert normalize_name("  IRFAN ") == "irfan"
    assert normalize_name("Rahul") == "rahul"
    assert normalize_name("null") == ""
    assert normalize_name("NULL") == ""


def test_missing_values():
    assert normalize_name(None) == ""
    assert normalize_mobile(" ") == ""
    assert normalize_mobile(None) == ""
    assert is_name_blank_or_null(None) is True
    assert is_name_blank_or_null("") is True
    assert is_name_blank_or_null("null") is True
    assert is_name_blank_or_null("NULL") is True
    assert is_name_blank_or_null("Rahul") is False


# ---------------------------------------------------------------------------
# Direct Engine/Connection Test Suite (Cases 1 - 11)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db():
    """Provides an isolated in-memory SQLite database."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name VARCHAR(100) NOT NULL,
                Mobile_Number VARCHAR(20)
            )
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_Number VARCHAR(50),
                Customer_ID BIGINT,
                Order_Amount FLOAT
            )
        """))
    return engine


def test_case_1_insert_customer_with_name_and_mobile(isolated_db):
    """Case 1: Name Rahul, mobile 9876543210 -> insert one customer."""
    with isolated_db.begin() as conn:
        cid, action = resolve_customer(conn, "Rahul", "9876543210", dialect_name="sqlite")
        assert action == "INSERTED"
        assert cid is not None

        row = conn.execute(text("SELECT Customer_Name, Mobile_Number FROM Customer WHERE Customer_ID = :id"), {"id": cid}).fetchone()
        assert row[0] == "Rahul"
        assert row[1] == "9876543210"


def test_case_2_same_mobile_different_name_is_new_customer(isolated_db):
    """Case 2: Name Amit, mobile 9876543210 -> new customer; Rahul's row is not touched."""
    with isolated_db.begin() as conn:
        cid1, action1 = resolve_customer(conn, "Rahul", "9876543210", dialect_name="sqlite")
        assert action1 == "INSERTED"

        cid2, action2 = resolve_customer(conn, "Amit", "9876543210", dialect_name="sqlite")
        assert action2 == "INSERTED"
        assert cid2 != cid1

        rows = conn.execute(text("SELECT Customer_ID, Customer_Name FROM Customer ORDER BY Customer_ID")).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(cid1, "Rahul"), (cid2, "Amit")]

        # Same name + same mobile -> reuse
        cid3, action3 = resolve_customer(conn, " rahul ", "9876543210", dialect_name="sqlite")
        assert action3 == "REUSED"
        assert cid3 == cid1


def test_case_3_reuse_customer_empty_name_existing_mobile(isolated_db):
    """Case 3: Empty name, mobile 9876543210 -> reuse the existing customer."""
    with isolated_db.begin() as conn:
        cid1, _ = resolve_customer(conn, "Rahul", "9876543210", dialect_name="sqlite")
        cid2, action2 = resolve_customer(conn, "", "9876543210", dialect_name="sqlite")
        assert action2 == "REUSED"
        assert cid2 == cid1

        cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert cnt == 1
        name = conn.execute(text("SELECT Customer_Name FROM Customer WHERE Customer_ID = :id"), {"id": cid1}).scalar()
        assert name == "Rahul"


def test_case_4_named_row_does_not_overwrite_unnamed_customer(isolated_db):
    """Case 4: Existing 'null'/9876543210; incoming Rahul/9876543210 -> new customer, existing row untouched."""
    with isolated_db.begin() as conn:
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('null', '9876543210')"))
        old_id = conn.execute(text("SELECT Customer_ID FROM Customer")).scalar()

        cid, action = resolve_customer(conn, "Rahul", "9876543210", dialect_name="sqlite")
        assert action == "INSERTED"
        assert cid != old_id
        assert conn.execute(text("SELECT Customer_Name FROM Customer WHERE Customer_ID = :id"), {"id": old_id}).scalar() == "null"

        # An unnamed row with that mobile still matches by mobile (prefers the unnamed customer).
        cid_blank, action_blank = resolve_customer(conn, "", "9876543210", dialect_name="sqlite")
        assert action_blank == "REUSED"
        assert cid_blank == old_id


def test_case_5_insert_name_only_customer(isolated_db):
    """Case 5: Name Rahul, empty mobile -> insert a name-only customer."""
    with isolated_db.begin() as conn:
        cid, action = resolve_customer(conn, "Rahul", None, dialect_name="sqlite")
        assert action == "INSERTED"

        row = conn.execute(text("SELECT Customer_Name, Mobile_Number FROM Customer WHERE Customer_ID = :id"), {"id": cid}).fetchone()
        assert row[0] == "Rahul"
        assert row[1] is None


def test_case_6_insert_separate_customer_for_different_name_empty_mobile(isolated_db):
    """Case 6: Name Amit, empty mobile -> insert a separate customer."""
    with isolated_db.begin() as conn:
        cid1, _ = resolve_customer(conn, "Rahul", "", dialect_name="sqlite")
        cid2, action2 = resolve_customer(conn, "Amit", "", dialect_name="sqlite")
        assert action2 == "INSERTED"
        assert cid2 != cid1

        cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert cnt == 2


def test_case_7_reuse_existing_name_only_customer(isolated_db):
    """Case 7: Name Rahul, empty mobile again -> reuse the existing name-only customer."""
    with isolated_db.begin() as conn:
        cid1, _ = resolve_customer(conn, "Rahul", None, dialect_name="sqlite")
        cid2, action2 = resolve_customer(conn, "Rahul", "", dialect_name="sqlite")
        assert action2 == "REUSED"
        assert cid2 == cid1

        cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert cnt == 1


def test_case_8_same_name_different_mobiles_remain_separate(isolated_db):
    """Case 8: Name Rahul, mobile 9123456780 -> create a separate customer from Rahul with mobile 9876543210."""
    with isolated_db.begin() as conn:
        cid1, _ = resolve_customer(conn, "Rahul", "9876543210", dialect_name="sqlite")
        cid2, action2 = resolve_customer(conn, "Rahul", "9123456780", dialect_name="sqlite")
        assert action2 == "INSERTED"
        assert cid2 != cid1

        cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert cnt == 2


def test_case_9_empty_name_new_mobile_stores_literal_null(isolated_db):
    """Case 9: Empty name, new mobile 9123456780 -> insert one customer with Customer_Name = 'null'."""
    with isolated_db.begin() as conn:
        cid, action = resolve_customer(conn, "", "9123456780", dialect_name="sqlite")
        assert action == "INSERTED"

        row = conn.execute(text("SELECT Customer_Name, Mobile_Number FROM Customer WHERE Customer_ID = :id"), {"id": cid}).fetchone()
        assert row[0] == "null"
        assert row[1] == "9123456780"


def test_case_10_empty_name_and_empty_mobile_skipped(isolated_db):
    """Case 10: Empty name and empty mobile -> skip the row."""
    with isolated_db.begin() as conn:
        cid, action = resolve_customer(conn, "", "", dialect_name="sqlite")
        assert action == "SKIPPED"
        assert cid is None

        cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert cnt == 0


def test_case_11_mobile_normalization_spaces_and_floats(isolated_db):
    """Case 11: Mobile values with surrounding spaces or Excel numeric/string representations -> normalize and match."""
    with isolated_db.begin() as conn:
        cid1, _ = resolve_customer(conn, "Rahul", "9876543210", dialect_name="sqlite")

        # Float representation like 9876543210.0
        cid2, act2 = resolve_customer(conn, "Rahul", 9876543210.0, dialect_name="sqlite")
        assert act2 == "REUSED"
        assert cid2 == cid1

        # String with spaces, same name
        cid3, act3 = resolve_customer(conn, "Rahul", "  9876543210  ", dialect_name="sqlite")
        assert act3 == "REUSED"
        assert cid3 == cid1

        cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert cnt == 1


# ---------------------------------------------------------------------------
# Full Migration Engine Integration Tests (Cases 12 - 15)
# ---------------------------------------------------------------------------

@pytest.fixture
def migration_test_schema():
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False),
        ColumnMetadata(name="Mobile_Number", type="VARCHAR", max_length=20, nullable=True),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_Number", type="VARCHAR", max_length=50, nullable=True),
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=True),
        ColumnMetadata(name="Order_Amount", type="FLOAT", nullable=True),
    ]
    order_tbl = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[
            ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
        ]
    )

    return DatabaseSchemaMetadata(
        database_name="POS_TEST",
        db_type="sqlite",
        tables=[customer_tbl, order_tbl]
    )


def test_case_12_repeated_rows_in_same_excel_file(isolated_db, migration_test_schema):
    """Case 12: Repeated rows in the same Excel file -> do not create duplicate customers."""
    df = pd.DataFrame([
        {"customer_name": "Rahul", "contact_phone": "9876543210", "order_no": "ORD-1", "amount": 100.0},
        {"customer_name": "Amit", "contact_phone": "9876543210", "order_no": "ORD-2", "amount": 150.0},
        {"customer_name": "", "contact_phone": "9876543210", "order_no": "ORD-3", "amount": 200.0},
        {"customer_name": "Rahul", "contact_phone": "9876543210", "order_no": "ORD-4", "amount": 250.0},
    ])

    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_no", databaseTable="Order", databaseColumn="Order_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Order_Amount", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=isolated_db), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=migration_test_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_repeated_customers")

    assert res.status == "COMPLETED"
    with isolated_db.connect() as conn:
        custs = conn.execute(text("SELECT Customer_ID, Customer_Name, Mobile_Number FROM Customer ORDER BY Customer_ID")).fetchall()
        # Rahul and Amit share a mobile but are different customers; the unnamed row
        # matches by mobile (first customer owning it); the repeated Rahul row is reused.
        assert [(c[1], c[2]) for c in custs] == [("Rahul", "9876543210"), ("Amit", "9876543210")]

        c_stat = res.tables["Customer"]
        assert c_stat.inserted == 2
        assert c_stat.reused == 2
        orders = conn.execute(text('SELECT Order_Number, Customer_ID FROM "Order" ORDER BY Order_ID')).fetchall()
        assert [o[1] for o in orders] == [custs[0][0], custs[1][0], custs[0][0], custs[0][0]]


def test_case_13_cross_run_deduplication(isolated_db, migration_test_schema):
    """Case 13: Existing database customers and incoming Excel customers -> match correctly across runs."""
    # Seed DB with existing customer
    with isolated_db.begin() as conn:
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('Rahul', '9876543210')"))

    # New migration run with same mobile but different name
    df = pd.DataFrame([
        {"customer_name": "Amit", "contact_phone": "9876543210", "order_no": "ORD-5", "amount": 300.0}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_no", databaseTable="Order", databaseColumn="Order_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Order_Amount", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=isolated_db), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=migration_test_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_cross_run")

    assert res.status == "COMPLETED"
    with isolated_db.connect() as conn:
        custs = conn.execute(text("SELECT Customer_ID, Customer_Name, Mobile_Number FROM Customer ORDER BY Customer_ID")).fetchall()
        assert [(c[1], c[2]) for c in custs] == [("Rahul", "9876543210"), ("Amit", "9876543210")]

        c_stat = res.tables["Customer"]
        assert c_stat.inserted == 1
        assert c_stat.reused == 0


def test_case_14_order_table_customer_id_assignment(isolated_db, migration_test_schema):
    """Case 14: Related orders -> verify that the correct reused or newly generated Customer_ID is assigned."""
    df = pd.DataFrame([
        {"customer_name": "Rahul", "contact_phone": "9876543210", "order_no": "ORD-101", "amount": 100.0},
        {"customer_name": "Amit", "contact_phone": "9876543210", "order_no": "ORD-102", "amount": 200.0},
        {"customer_name": "Priya", "contact_phone": "9123456789", "order_no": "ORD-103", "amount": 300.0},
        {"customer_name": "", "contact_phone": "", "order_no": "ORD-104", "amount": 400.0},
    ])

    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_no", databaseTable="Order", databaseColumn="Order_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Order_Amount", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=isolated_db), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=migration_test_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_order_fk_assignment")

    assert res.status == "COMPLETED"
    with isolated_db.connect() as conn:
        orders = conn.execute(text("SELECT Order_Number, Customer_ID FROM \"Order\" ORDER BY Order_ID")).fetchall()
        assert len(orders) == 4

        rahul_cid = conn.execute(text("SELECT Customer_ID FROM Customer WHERE Customer_Name = 'Rahul'")).scalar()
        amit_cid = conn.execute(text("SELECT Customer_ID FROM Customer WHERE Customer_Name = 'Amit'")).scalar()
        priya_cid = conn.execute(text("SELECT Customer_ID FROM Customer WHERE Mobile_Number = '9123456789'")).scalar()

        # Rahul and Amit share a mobile but are different customers with their own IDs
        assert orders[0][1] == rahul_cid
        assert orders[1][1] == amit_cid
        assert rahul_cid != amit_cid

        # ORD-103 must have Priya's Customer_ID
        assert orders[2][1] == priya_cid

        # ORD-104 had both customer name and mobile empty, customer skipped, order has Customer_ID = NULL
        assert orders[3][1] is None


def test_case_15_simulated_database_failure_transaction_rollback(isolated_db, migration_test_schema):
    """Case 15: Simulated database failure -> verify transaction rollback prevents partial records."""
    from contextlib import contextmanager
    from app.migration.transaction import transaction_manager

    df = pd.DataFrame([
        {"customer_name": "Rahul", "contact_phone": "9876543210", "order_no": "ORD-FAIL-1", "amount": 100.0},
    ])

    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_no", databaseTable="Order", databaseColumn="Order_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Order_Amount", status="MATCHED"),
    ]

    # Simulate DB failure during execution_fn inside execute_transactionally
    original_exec = transaction_manager.execute_transactionally

    def failing_execute_transactionally(upload_id, engine, execution_fn):
        def intercepting_fn(conn):
            orig_execute = conn.execute
            def intercepting_execute(statement, *args, **kwargs):
                stmt_str = str(statement).upper()
                if "INSERT INTO" in stmt_str and ("ORDER" in stmt_str or '"ORDER"' in stmt_str):
                    raise RuntimeError("Simulated DB failure during Order insertion")
                return orig_execute(statement, *args, **kwargs)
            conn.execute = intercepting_execute
            return execution_fn(conn)
        return original_exec(upload_id, engine, intercepting_fn)

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=isolated_db), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=migration_test_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("app.migration.transaction.transaction_manager.execute_transactionally", side_effect=failing_execute_transactionally), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}

        with pytest.raises(RuntimeError) as exc_info:
            MigrationEngine.execute_migration("upload_test_rollback")
        assert "Simulated DB failure" in str(exc_info.value)
        # Verify transaction manager tracked the rolled back execution
        cached = transaction_manager.get_completed("upload_test_rollback")
        assert cached is not None
        assert cached.transaction == "ROLLED_BACK"

    # Confirm that transaction rollback occurred and no partial rows were committed to Customer or Order
    with isolated_db.connect() as conn:
        c_cnt = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        o_cnt = conn.execute(text("SELECT COUNT(*) FROM \"Order\"")).scalar()
        assert c_cnt == 0
        assert o_cnt == 0


def test_case_16_customer_date_added_day_first_formatting_normalized():
    """Case 16: Verify day-first datetime strings like '30-06-2026 00:00' are normalized to ISO format."""
    db = create_engine("sqlite:///:memory:")
    with db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name VARCHAR(100) NOT NULL,
                Mobile_Number VARCHAR(20),
                Date_Added DATETIME,
                Address VARCHAR(200)
            )
        """))

    schema = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Customer",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False),
                    ColumnMetadata(name="Mobile_Number", type="VARCHAR(20)", nullable=True),
                    ColumnMetadata(name="Date_Added", type="DATETIME", nullable=True),
                    ColumnMetadata(name="Address", type="VARCHAR(200)", nullable=True),
                ],
                primary_keys=["Customer_ID"]
            )
        ]
    )

    df = pd.DataFrame([
        {
            "customer_name": "irfan",
            "contact_phone": "9876543210",
            "customer_date": "30-06-2026 00:00",
            "address": "Kumaraswamy Layout Bengaluru India"
        },
        {
            "customer_name": "priya",
            "contact_phone": "9123456789",
            "customer_date": "15/07/2026 18:30",
            "address": "Whitefield Bengaluru"
        },
        {
            "customer_name": "anand",
            "contact_phone": "9988776655",
            "customer_date": None,
            "address": "Indiranagar"
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="customer_date", databaseTable="Customer", databaseColumn="Date_Added", status="MATCHED"),
        ColumnMappingResult(excelColumn="address", databaseTable="Customer", databaseColumn="Address", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=db), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        response = MigrationEngine.execute_migration("upload_test_date_normalization")

    assert response.status == "COMPLETED"
    assert response.transaction == "COMMITTED"

    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("SELECT Customer_Name, Mobile_Number, Date_Added, Address FROM Customer ORDER BY Customer_ID")).mappings().all()]

    assert len(rows) == 3
    # 1. irfan -> Date_Added normalized from '30-06-2026 00:00' to '2026-06-30 00:00:00'
    assert rows[0]["Customer_Name"] == "irfan"
    assert rows[0]["Date_Added"] == "2026-06-30 00:00:00"
    assert rows[0]["Address"] == "Kumaraswamy Layout Bengaluru India"

    # 2. priya -> Date_Added normalized from '15/07/2026 18:30' to '2026-07-15 18:30:00'
    assert rows[1]["Customer_Name"] == "priya"
    assert rows[1]["Date_Added"] == "2026-07-15 18:30:00"

    # 3. anand -> Date_Added is None
    assert rows[2]["Customer_Name"] == "anand"
    assert rows[2]["Date_Added"] is None

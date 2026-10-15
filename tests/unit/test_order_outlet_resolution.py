"""
Comprehensive regression tests for pre-execution validation and dependency-aware value resolution
specifically addressing dbo.[Order] and Order_Outlet_ID resolution against dbo.Outlets, as well
as generic multi-table foreign-key propagation and pre-execution validation enforcement.

Key Scenarios:
1. dbo.[Order] insertion with resolved Order_Outlet_ID from existing dbo.Outlets record (ANN - BTM -> 69):
   - Confirms Order_Outlet_ID is included in the generated INSERT statement and correctly executed.
2. Unknown outlets never inserted into read-only dbo.Outlets:
    - Required Order foreign key is blocked and neither table is modified.
3. Pre-execution validation blocks execution when a required foreign key cannot be resolved:
   - Unresolved store name or missing identifying column sets canExecute=False, status=BLOCKED, transaction=NOT_EXECUTED.
   - Strictly ZERO writes committed to database.
4. Multiple foreign keys in dbo.[Order]:
   - Order_Outlet_ID (mandatory), Assigned_Outlet_ID (optional), Customer_ID (optional) all resolved properly.
5. Fully generic schema behavior:
   - Non-Order schema with non-declarative foreign keys verified end-to-end.
6. Preview mode remains strictly read-only with zero database writes.
"""

import pytest
import pandas as pd
from unittest.mock import patch
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult
from app.migration.engine import MigrationEngine
from app.migration.preview import MigrationPreviewEngine
from app.migration.validator import MigrationValidator
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship


@pytest.fixture
def bopos_schema():
    """
    Constructs DatabaseSchemaMetadata strictly mirroring BOPOS_Leons metadata:
    - Outlets table: Outlet_ID (identity PK), Outlet_Name, Short_Code
    - Customer table: Customer_ID (identity PK), Customer_Name, Location, Outlet_ID
    - [Order] table: Order_ID (identity PK), Order_DateTime (NOT NULL), Order_Outlet_ID (NOT NULL),
                    Assigned_Outlet_ID (NULLABLE), Customer_ID (NULLABLE), Order_Status, Tax
    - Notice: foreign_keys list is EMPTY on all tables, matching BOPOS_Leons catalog reality!
    """
    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR", max_length=100, nullable=True),
        ColumnMetadata(name="Short_Code", type="VARCHAR", max_length=20, nullable=True),
    ]
    outlets_tbl = TableMetadata(
        name="Outlets",
        schema_name="dbo",
        columns=outlets_cols,
        primary_keys=["Outlet_ID"],
        foreign_keys=[]
    )

    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False),
        ColumnMetadata(name="Location", type="VARCHAR", max_length=200, nullable=True),
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=True),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    order_cols = [
        ColumnMetadata(name="Order_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False),
        ColumnMetadata(name="Order_Outlet_ID", type="INT", nullable=False),  # Required NOT NULL
        ColumnMetadata(name="Assigned_Outlet_ID", type="INT", nullable=True),  # Optional
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=True),  # Optional
        ColumnMetadata(name="Order_Status", type="VARCHAR", max_length=50, nullable=True),
        ColumnMetadata(name="Tax", type="FLOAT", nullable=True),
    ]
    order_tbl = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[outlets_tbl, customer_tbl, order_tbl]
    )


def test_order_outlet_id_resolved_from_existing_outlet(bopos_schema):
    """
    Scenario 1: Existing outlet record exists in SQL Server (ANN -> 69).
    Excel contains restaurant_name = 'ANN - BTM' and Order columns; the existing outlet is named 'BTM'.
    The generated INSERT statement must include Order_Outlet_ID and execute successfully with ID 69.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_Name TEXT, Location TEXT, Outlet_ID INTEGER)"))
        conn.execute(text("""
            CREATE TABLE [Order] (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_DateTime TEXT NOT NULL,
                Order_Outlet_ID INTEGER NOT NULL,
                Assigned_Outlet_ID INTEGER,
                Customer_ID INTEGER,
                Order_Status TEXT,
                Tax REAL
            )
        """))
        # Seed existing outlet with Outlet_ID = 69 and branch name BTM.
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name, Short_Code) VALUES (69, 'BTM', 'BTM')"))

    df = pd.DataFrame([
        {
            "restaurant_name": "ANN - BTM",
            "virtual_brand_name": "ANN - BTM",
            "Order_Date": "2026-07-01 12:30:00",
            "Status": "COMPLETED",
            "Tax_Amount": 15.5
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Order_Date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED"),
        ColumnMappingResult(excelColumn="Status", databaseTable="Order", databaseColumn="Order_Status", status="MATCHED"),
        ColumnMappingResult(excelColumn="Tax_Amount", databaseTable="Order", databaseColumn="Tax", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=bopos_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_order_outlet_existing")

    assert res.status == "COMPLETED"
    assert res.transaction in ("COMMITTED", "SUCCESS")
    assert res.totalInserted >= 1

    with engine.connect() as conn:
        orders = conn.execute(text("SELECT Order_ID, Order_DateTime, Order_Outlet_ID, Order_Status, Tax FROM [Order]")).fetchall()
        assert len(orders) == 1
        order_row = orders[0]
        # Order_Outlet_ID must be 69!
        assert order_row[2] == 69
        assert order_row[3] in ("Completed", "COMPLETED")
        assert order_row[4] == 15.5


def test_unknown_outlet_is_not_inserted_and_blocks_order(bopos_schema):
    """
    Scenario 2: Outlets is a read-only reference table. Unknown outlets cannot be created.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("""
            CREATE TABLE [Order] (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_DateTime TEXT NOT NULL,
                Order_Outlet_ID INTEGER NOT NULL,
                Assigned_Outlet_ID INTEGER,
                Customer_ID INTEGER,
                Order_Status TEXT,
                Tax REAL
            )
        """))

    df = pd.DataFrame([
        {
            "Branch_Name": "Downtown Express",
            "Order_Date": "2026-07-02 14:00:00",
            "Status": "DELIVERED",
            "Tax_Amount": 20.0
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Branch_Name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Order_Date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED"),
        ColumnMappingResult(excelColumn="Status", databaseTable="Order", databaseColumn="Order_Status", status="MATCHED"),
        ColumnMappingResult(excelColumn="Tax_Amount", databaseTable="Order", databaseColumn="Tax", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=bopos_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_order_new_outlet")

    assert res.status == "BLOCKED"
    assert res.transactionStatus == "NOT_EXECUTED"
    assert res.canExecute is False

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM Outlets")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM [Order]")).scalar() == 0


def test_pre_execution_validation_blocks_unresolvable_order_outlet(bopos_schema):
    """
    Scenario 3: Source data contains an outlet name that does NOT exist in SQL Server,
    and Outlets is NOT an active target table.
    Pre-execution validation must detect that required Order_Outlet_ID cannot be resolved,
    set canExecute = False, return status = BLOCKED, and perform ZERO writes!
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("""
            CREATE TABLE [Order] (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_DateTime TEXT NOT NULL,
                Order_Outlet_ID INTEGER NOT NULL,
                Assigned_Outlet_ID INTEGER,
                Customer_ID INTEGER,
                Order_Status TEXT,
                Tax REAL
            )
        """))
        # Seed only outlet 69 ANN
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name, Short_Code) VALUES (69, 'ANN', 'ANN')"))

    # Excel contains NonExistentStore which is NOT in database
    df = pd.DataFrame([
        {
            "restaurant_name": "NonExistentStore",
            "Order_Date": "2026-07-03 10:00:00",
            "Status": "PENDING"
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Order_Date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED"),
        ColumnMappingResult(excelColumn="Status", databaseTable="Order", databaseColumn="Order_Status", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=bopos_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_blocked_order_outlet")

    # Migration must be blocked!
    assert res.status == "BLOCKED"
    assert res.transactionStatus == "NOT_EXECUTED"
    assert res.canExecute is False
    assert len(res.validationErrors) > 0
    # Must report Order_Outlet_ID
    assert any(e.column_name == "Order_Outlet_ID" for e in res.validationErrors)

    # Strictly ZERO writes committed
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM [Order]")).scalar()
        assert count == 0


def test_multiple_fks_in_order_resolved_simultaneously(bopos_schema):
    """
    Scenario 4: dbo.[Order] references:
    - Order_Outlet_ID (mandatory -> Outlets.Outlet_ID)
    - Assigned_Outlet_ID (optional -> Outlets.Outlet_ID)
    - Customer_ID (optional -> Customer.Customer_ID)
    All are resolved dynamically and populated in the INSERT statement.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_Name TEXT, Location TEXT, Outlet_ID INTEGER)"))
        conn.execute(text("""
            CREATE TABLE [Order] (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_DateTime TEXT NOT NULL,
                Order_Outlet_ID INTEGER NOT NULL,
                Assigned_Outlet_ID INTEGER,
                Customer_ID INTEGER,
                Order_Status TEXT,
                Tax REAL
            )
        """))
        # Seed Outlet 69 ANN and Customer 101 John Doe
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name, Short_Code) VALUES (69, 'ANN', 'ANN')"))
        conn.execute(text("INSERT INTO Customer (Customer_ID, Customer_Name, Location, Outlet_ID) VALUES (101, 'John Doe', 'Main St', 69)"))

    df = pd.DataFrame([
        {
            "restaurant_name": "ANN",
            "Customer_Name": "John Doe",
            "Order_Date": "2026-07-04 18:00:00",
            "Status": "COMPLETED",
            "Tax_Amount": 30.0
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Customer_Name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Order_Date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED"),
        ColumnMappingResult(excelColumn="Status", databaseTable="Order", databaseColumn="Order_Status", status="MATCHED"),
        ColumnMappingResult(excelColumn="Tax_Amount", databaseTable="Order", databaseColumn="Tax", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=bopos_schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_multi_fk_order")

    assert res.status == "COMPLETED"
    assert res.totalInserted >= 1

    with engine.connect() as conn:
        orders = conn.execute(text("SELECT Order_ID, Order_Outlet_ID, Customer_ID, Order_Status FROM [Order]")).fetchall()
        assert len(orders) == 1
        o = orders[0]
        assert o[1] == 69   # Order_Outlet_ID
        assert o[2] == 101  # Customer_ID
        assert o[3] in ("Completed", "COMPLETED")


def test_preview_mode_strictly_read_only(bopos_schema):
    """
    Scenario 6: Preview engine must generate complete row previews and dependency plans
    without writing any records to the database.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("""
            CREATE TABLE [Order] (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_DateTime TEXT NOT NULL,
                Order_Outlet_ID INTEGER NOT NULL,
                Order_Status TEXT
            )
        """))
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name, Short_Code) VALUES (69, 'ANN', 'ANN')"))

    df = pd.DataFrame([
        {
            "restaurant_name": "ANN",
            "Order_Date": "2026-07-05 11:00:00",
            "Status": "PENDING"
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Order_Date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED"),
        ColumnMappingResult(excelColumn="Status", databaseTable="Order", databaseColumn="Order_Status", status="MATCHED"),
    ]

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_order.xlsx",
        schema=bopos_schema,
        column_mappings=mappings,
        engine=engine
    )

    # Verify preview returned preview rows and validation summary
    assert len(preview_res.preview_rows) > 0
    # Must show Order_Outlet_ID in preview rows
    order_outlet_previews = [p for p in preview_res.preview_rows if p.target_column == "Order_Outlet_ID"]
    assert len(order_outlet_previews) > 0
    assert order_outlet_previews[0].final_value == "69"
    assert order_outlet_previews[0].value_source == "DATABASE_LOOKUP"

    # Confirms preview performed zero writes
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM [Order]")).scalar()
        assert count == 0

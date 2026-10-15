"""
Comprehensive unit and integration tests for fully generic, dependency-aware ID resolution and data migration.

Scenarios tested:
1. IdResolverStore unit tests:
   - Normalization and basic ID lookup (register_id / get_id)
   - Row-level ID registration and retrieval (register_row_id, get_row_id, has_row_id)
   - Composite key registration and resolution (register_composite_key, get_composite_id)
   - Distinct IDs tracking and comprehensive audit report generation
2. Multi-level dependency hierarchy (Grandparent -> Parent -> Child):
   - Departments -> Teams -> Members (Level 1 PK -> Level 2 FK -> Level 2 PK -> Level 3 FK)
3. Multiple foreign keys in a single table:
   - Orders referencing both Customers and Outlets
4. Repeated references to the same parent table:
   - Transfers referencing Warehouses via From_Warehouse_ID and To_Warehouse_ID
5. Deduplication of parents and existing record reuse:
   - Multiple Excel rows sharing a parent entity; existing parents reused, new parents inserted
6. SQL Server trigger fallback:
   - Catches trigger error on OUTPUT clause and falls back to SCOPE_IDENTITY()
7. Self-referencing table relationships:
   - Employees referencing Manager_ID in the same Employees table
8. Unresolved foreign key handling and automatic transaction rollback:
   - Ensures failure on missing required FK leaves zero partial inserts
9. MigrationPreviewEngine read-only preview:
   - Verifies preview tagging and zero database writes
"""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult, ResolvedEntityRecord
from app.migration.id_resolver import IdResolver, IdResolverStore
from app.migration.engine import MigrationEngine
from app.migration.dependency import DependencyResolver
from app.migration.preview import MigrationPreviewEngine
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship


# -----------------------------------------------------------------------------
# 1. IdResolverStore Core Unit Tests
# -----------------------------------------------------------------------------

def test_id_resolver_basic_and_normalization():
    resolver = IdResolverStore()
    resolver.register_id("dbo.Customers", "Acme Corp", 101)
    
    # Resolves with exact string
    assert resolver.get_id("dbo.Customers", "Acme Corp") == 101
    # Resolves with case/whitespace variations
    assert resolver.get_id("dbo.Customers", "  acme corp  ") == 101
    # Integer key resolution
    resolver.register_id("dbo.Accounts", 5001, "ACC-5001")
    assert resolver.get_id("dbo.Accounts", "5001") == "ACC-5001"
    assert resolver.get_id("dbo.Accounts", 5001) == "ACC-5001"


def test_id_resolver_row_level_mappings():
    resolver = IdResolverStore()
    resolver.register_row_id("dbo.Customers", 0, 101)
    resolver.register_row_id("dbo.Customers", 1, 102)
    resolver.register_row_id("dbo.Customers", 2, 101) # row 2 shares same customer as row 0

    assert resolver.has_row_id("dbo.Customers", 0) is True
    assert resolver.has_row_id("dbo.Customers", 3) is False
    assert resolver.get_row_id("dbo.Customers", 0) == 101
    assert resolver.get_row_id("dbo.Customers", 1) == 102
    assert resolver.get_row_id("dbo.Customers", 2) == 101


def test_id_resolver_composite_keys():
    resolver = IdResolverStore()
    resolver.register_composite_key("dbo.Stores", ("East", "E-01"), 42)
    
    # Match exact composite
    assert resolver.get_composite_id("dbo.Stores", ("East", "E-01")) == 42
    # Non-matching composite
    assert resolver.get_composite_id("dbo.Stores", ("West", "E-01")) is None


def test_id_resolver_distinct_ids_and_report():
    resolver = IdResolverStore()
    resolver.register_id("dbo.Customers", "CustA", 1)
    resolver.register_id("dbo.Customers", "CustB", 2)
    resolver.register_row_id("dbo.Customers", 0, 1)
    resolver.register_row_id("dbo.Customers", 1, 2)
    resolver.register_row_id("dbo.Customers", 2, 1)

    distinct = resolver.get_distinct_ids("dbo.Customers")
    assert set(distinct) == {1, 2}

    report = resolver.get_resolution_report()
    assert "dbo.Customers" in report
    assert report["dbo.Customers"]["distinct_database_ids"] == 2
    assert report["dbo.Customers"]["rows_mapped"] == 3
    assert report["dbo.Customers"]["business_keys_indexed"] >= 2


# -----------------------------------------------------------------------------
# 2. Multi-level Dependency Hierarchy (Grandparent -> Parent -> Child)
# -----------------------------------------------------------------------------

def test_multi_level_hierarchy_migration():
    """
    Test 3-level chain:
    Departments (PK: Dept_ID) -> Teams (PK: Team_ID, FK: Dept_ID) -> Members (PK: Member_ID, FK: Team_ID)
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Departments (Dept_ID INTEGER PRIMARY KEY AUTOINCREMENT, Dept_Name TEXT UNIQUE)"))
        conn.execute(text("CREATE TABLE Teams (Team_ID INTEGER PRIMARY KEY AUTOINCREMENT, Dept_ID INTEGER, Team_Name TEXT UNIQUE, FOREIGN KEY(Dept_ID) REFERENCES Departments(Dept_ID))"))
        conn.execute(text("CREATE TABLE Members (Member_ID INTEGER PRIMARY KEY AUTOINCREMENT, Team_ID INTEGER, Member_Name TEXT, FOREIGN KEY(Team_ID) REFERENCES Teams(Team_ID))"))

    fk_team_dept = ForeignKeyInfo(constrained_column="Dept_ID", target_table="Departments", target_column="Dept_ID", target_schema="dbo")
    fk_member_team = ForeignKeyInfo(constrained_column="Team_ID", target_table="Teams", target_column="Team_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="OrgDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Departments", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Dept_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Dept_Name", type="VARCHAR", nullable=False, primary_key=False, unique=True)
                ],
                primary_keys=["Dept_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Teams", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Team_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Dept_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_team_dept]),
                    ColumnMetadata(name="Team_Name", type="VARCHAR", nullable=False, primary_key=False, unique=True)
                ],
                primary_keys=["Team_ID"], foreign_keys=[fk_team_dept]
            ),
            TableMetadata(
                name="Members", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Member_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Team_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_member_team]),
                    ColumnMetadata(name="Member_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Member_ID"], foreign_keys=[fk_member_team]
            )
        ]
    )

    # Topological sort check
    order = DependencyResolver.resolve_execution_order(schema_metadata, target_tables=["Members", "Departments", "Teams"])
    assert order == ["Departments", "Teams", "Members"]

    # Excel DataFrame representing 3 members across 2 teams in 1 department
    df = pd.DataFrame([
        {"Department": "Engineering", "Team": "Core AI", "Person": "Alice"},
        {"Department": "Engineering", "Team": "Core AI", "Person": "Bob"},
        {"Department": "Engineering", "Team": "Frontend", "Person": "Charlie"}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Department", databaseTable="Departments", databaseColumn="Dept_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Team", databaseTable="Teams", databaseColumn="Team_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Person", databaseTable="Members", databaseColumn="Member_Name", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_multilevel_test")

    assert res.status == "COMPLETED"
    assert res.transaction == "COMMITTED"
    assert res.totalInserted == 6  # 1 unique Dept + 2 unique Teams + 3 Members

    with engine.connect() as conn:
        depts = conn.execute(text("SELECT Dept_ID, Dept_Name FROM Departments")).fetchall()
        assert len(depts) == 1
        dept_id = depts[0][0]
        assert depts[0][1] == "Engineering"

        teams = conn.execute(text("SELECT Team_ID, Dept_ID, Team_Name FROM Teams")).fetchall()
        assert len(teams) == 2
        for t in teams:
            assert t[1] == dept_id  # Both teams reference the engineering dept

        members = conn.execute(text("SELECT Member_ID, Team_ID, Member_Name FROM Members")).fetchall()
        assert len(members) == 3
        team_dict = {t[2]: t[0] for t in teams}
        for m in members:
            if m[2] in ["Alice", "Bob"]:
                assert m[1] == team_dict["Core AI"]
            elif m[2] == "Charlie":
                assert m[1] == team_dict["Frontend"]


# -----------------------------------------------------------------------------
# 3. Multiple Foreign Keys in a Single Table
# -----------------------------------------------------------------------------

def test_multiple_fks_in_single_table():
    """
    Test Orders table having FKs to both Customers and Outlets.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Customers (Cust_ID INTEGER PRIMARY KEY AUTOINCREMENT, Cust_Name TEXT)"))
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT UNIQUE)"))
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name) VALUES (1, 'Downtown'), (2, 'Uptown')"))
        conn.execute(text("CREATE TABLE Orders (Order_ID INTEGER PRIMARY KEY AUTOINCREMENT, Cust_ID INTEGER, Outlet_ID INTEGER, Total REAL)"))

    fk_cust = ForeignKeyInfo(constrained_column="Cust_ID", target_table="Customers", target_column="Cust_ID", target_schema="dbo")
    fk_outlet = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="SalesDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Customers", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Cust_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Cust_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Cust_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Outlets", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Outlet_Name", type="VARCHAR", nullable=False, primary_key=False, unique=True)
                ],
                primary_keys=["Outlet_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Orders", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Cust_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_cust]),
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_outlet]),
                    ColumnMetadata(name="Total", type="FLOAT", nullable=False, primary_key=False)
                ],
                primary_keys=["Order_ID"], foreign_keys=[fk_cust, fk_outlet]
            )
        ]
    )

    df = pd.DataFrame([
        {"Customer": "Acme Inc", "Location": "Downtown", "Amount": 150.0},
        {"Customer": "Beta LLC", "Location": "Uptown", "Amount": 275.5}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Customer", databaseTable="Customers", databaseColumn="Cust_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Location", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Amount", databaseTable="Orders", databaseColumn="Total", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_multi_fk_test")

    assert res.status == "COMPLETED"
    assert res.totalInserted == 4  # Outlets are read-only; insert 2 customers + 2 orders

    with engine.connect() as conn:
        orders = conn.execute(text("SELECT Order_ID, Cust_ID, Outlet_ID, Total FROM Orders")).fetchall()
        assert len(orders) == 2
        assert conn.execute(text("SELECT COUNT(*) FROM Outlets")).scalar_one() == 2
        for o in orders:
            assert o[1] is not None and o[1] > 0
            assert o[2] is not None and o[2] > 0


# -----------------------------------------------------------------------------
# 4. Repeated References to Same Parent Table
# -----------------------------------------------------------------------------

def test_repeated_references_to_same_parent():
    """
    Test Transfers table referencing Warehouses twice:
    From_Warehouse_ID -> Warehouses.Warehouse_ID
    To_Warehouse_ID -> Warehouses.Warehouse_ID
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Warehouses (Warehouse_ID INTEGER PRIMARY KEY AUTOINCREMENT, Warehouse_Name TEXT UNIQUE)"))
        conn.execute(text("CREATE TABLE Transfers (Transfer_ID INTEGER PRIMARY KEY AUTOINCREMENT, From_Warehouse_ID INTEGER, To_Warehouse_ID INTEGER, Qty INTEGER)"))
        # Seed existing warehouses
        conn.execute(text("INSERT INTO Warehouses (Warehouse_ID, Warehouse_Name) VALUES (1, 'North Hub'), (2, 'South Hub')"))

    fk_from = ForeignKeyInfo(constrained_column="From_Warehouse_ID", target_table="Warehouses", target_column="Warehouse_ID", target_schema="dbo")
    fk_to = ForeignKeyInfo(constrained_column="To_Warehouse_ID", target_table="Warehouses", target_column="Warehouse_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="LogisticsDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Warehouses", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Warehouse_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Warehouse_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Warehouse_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Transfers", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Transfer_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="From_Warehouse_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_from]),
                    ColumnMetadata(name="To_Warehouse_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_to]),
                    ColumnMetadata(name="Qty", type="INT", nullable=False, primary_key=False)
                ],
                primary_keys=["Transfer_ID"], foreign_keys=[fk_from, fk_to]
            )
        ]
    )

    df = pd.DataFrame([
        {"Origin": "North Hub", "Destination": "South Hub", "Quantity": 50}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Origin", databaseTable="Transfers", databaseColumn="From_Warehouse_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="Destination", databaseTable="Transfers", databaseColumn="To_Warehouse_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="Quantity", databaseTable="Transfers", databaseColumn="Qty", status="MATCHED")
    ]


    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {
            "filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1,
            "relationships": [
                {
                    "childTable": "Transfers", "childColumn": "From_Warehouse_ID",
                    "parentTable": "Warehouses", "parentColumn": "Warehouse_ID",
                    "parentNameColumn": "Warehouse_Name", "excelNameColumn": "Origin"
                },
                {
                    "childTable": "Transfers", "childColumn": "To_Warehouse_ID",
                    "parentTable": "Warehouses", "parentColumn": "Warehouse_ID",
                    "parentNameColumn": "Warehouse_Name", "excelNameColumn": "Destination"
                }
            ]
        }
        res = MigrationEngine.execute_migration("upload_repeated_fk_test")

    assert res.status == "COMPLETED"
    with engine.connect() as conn:
        transfers = conn.execute(text("SELECT Transfer_ID, From_Warehouse_ID, To_Warehouse_ID, Qty FROM Transfers")).fetchall()
        assert len(transfers) == 1
        assert transfers[0][1] == 1  # North Hub
        assert transfers[0][2] == 2  # South Hub
        assert transfers[0][3] == 50


# -----------------------------------------------------------------------------
# 5. Existing Record Reuse with Child ID Propagation
# -----------------------------------------------------------------------------

def test_existing_record_reuse_and_id_propagation():
    """
    Test when parent already exists in DB -> its existing ID is retrieved and reused,
    and child rows reference that reused ID without creating duplicates.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Vendors (Vendor_ID INTEGER PRIMARY KEY AUTOINCREMENT, Vendor_Name TEXT UNIQUE)"))
        conn.execute(text("CREATE TABLE Products (Product_ID INTEGER PRIMARY KEY AUTOINCREMENT, Vendor_ID INTEGER, Prod_Name TEXT)"))
        # Seed existing vendor
        conn.execute(text("INSERT INTO Vendors (Vendor_ID, Vendor_Name) VALUES (77, 'Existing Global Supplier')"))

    fk_vendor = ForeignKeyInfo(constrained_column="Vendor_ID", target_table="Vendors", target_column="Vendor_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="InventoryDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Vendors", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Vendor_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Vendor_Name", type="VARCHAR", nullable=False, primary_key=False, unique=True)
                ],
                primary_keys=["Vendor_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Products", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Product_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Vendor_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_vendor]),
                    ColumnMetadata(name="Prod_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Product_ID"], foreign_keys=[fk_vendor]
            )
        ]
    )

    df = pd.DataFrame([
        {"Supplier": "Existing Global Supplier", "Item": "Widget Alpha"},
        {"Supplier": "Existing Global Supplier", "Item": "Widget Beta"},
        {"Supplier": "Brand New Supplier", "Item": "Widget Gamma"}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Supplier", databaseTable="Vendors", databaseColumn="Vendor_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Item", databaseTable="Products", databaseColumn="Prod_Name", status="MATCHED")
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_reuse_test")

    assert res.status == "COMPLETED"
    assert res.totalReused == 1  # 1 vendor reused
    assert res.totalInserted == 4 # 1 new vendor + 3 products

    with engine.connect() as conn:
        vendors = conn.execute(text("SELECT Vendor_ID, Vendor_Name FROM Vendors ORDER BY Vendor_ID")).fetchall()
        assert len(vendors) == 2  # No duplicate vendor created
        new_vendor_id = [v[0] for v in vendors if v[1] == "Brand New Supplier"][0]

        prods = conn.execute(text("SELECT Product_ID, Vendor_ID, Prod_Name FROM Products ORDER BY Product_ID")).fetchall()
        assert len(prods) == 3
        assert prods[0][1] == 77  # Widget Alpha linked to reused vendor 77
        assert prods[1][1] == 77  # Widget Beta linked to reused vendor 77
        assert prods[2][1] == new_vendor_id  # Widget Gamma linked to new vendor


# -----------------------------------------------------------------------------
# 6. SQL Server Trigger Fallback (OUTPUT clause error -> SCOPE_IDENTITY fallback)
# -----------------------------------------------------------------------------

def test_sqlserver_trigger_fallback_to_scope_identity():
    """
    Test that when an insert with OUTPUT INSERTED.pk fails due to trigger restrictions
    (e.g., 'The target table has run-time triggers and cannot have an OUTPUT clause without INTO'),
    the engine catches the error, retries with SCOPE_IDENTITY() batch fallback.
    """
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.name = "mssql"

    # Simulate first execute raising trigger exception, second succeeding (SCOPE_IDENTITY fallback)
    trigger_error = Exception("The target table 'Orders' has run-time triggers and cannot have an OUTPUT clause without INTO")
    mock_cursor_scope = MagicMock()
    mock_cursor_scope.scalar.return_value = 999

    mock_conn.execute.side_effect = [
        trigger_error,          # Attempt 1: OUTPUT INSERTED fail
        mock_cursor_scope       # Attempt 2: SCOPE_IDENTITY fallback batch
    ]

    schema_metadata = DatabaseSchemaMetadata(
        database_name="OrdersDB",
        db_type="mssql",
        tables=[
            TableMetadata(
                name="Orders", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Notes", type="VARCHAR", nullable=True)
                ],
                primary_keys=["Order_ID"], foreign_keys=[]
            )
        ]
    )

    df = pd.DataFrame([{"OrderNotes": "Urgent order"}])
    mappings = [
        ColumnMappingResult(excelColumn="OrderNotes", databaseTable="Orders", databaseColumn="Notes", status="MATCHED")
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=mock_engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("app.migration.transaction.transaction_manager.execute_transactionally") as mock_tm, \
         patch("os.path.exists", return_value=True):
        
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        # Run execution callback directly using mock_conn
        def run_action(*args, **kwargs):
            fn = kwargs.get("execution_fn") or (args[2] if len(args) > 2 else None)
            return fn(mock_conn)
        mock_tm.side_effect = run_action


        res = MigrationEngine.execute_migration("upload_trigger_test")

    assert res.status == "COMPLETED"
    assert res.totalInserted == 1
    # Confirms execution calls were made: 1st failed with trigger error, subsequent succeeded with fallback
    assert mock_conn.execute.call_count >= 2



# -----------------------------------------------------------------------------
# 7. Self-referencing Relationships
# -----------------------------------------------------------------------------

def test_self_referencing_relationship():
    """
    Test an Employees table where Manager_ID references Employee_ID in the same table.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Employees (Employee_ID INTEGER PRIMARY KEY AUTOINCREMENT, Manager_ID INTEGER, Name TEXT)"))
        # Existing manager
        conn.execute(text("INSERT INTO Employees (Employee_ID, Manager_ID, Name) VALUES (1, NULL, 'Alice Director')"))

    fk_self = ForeignKeyInfo(constrained_column="Manager_ID", target_table="Employees", target_column="Employee_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="HRDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Employees", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Employee_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Manager_ID", type="INT", nullable=True, primary_key=False, foreign_keys=[fk_self]),
                    ColumnMetadata(name="Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Employee_ID"], foreign_keys=[fk_self]
            )
        ]
    )

    df = pd.DataFrame([
        {"EmpName": "Bob Developer", "Manager": "Alice Director"}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="EmpName", databaseTable="Employees", databaseColumn="Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Manager", databaseTable="Employees", databaseColumn="Manager_ID", status="MATCHED")
    ]


    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {
            "filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1,
            "relationships": [
                {
                    "childTable": "Employees", "childColumn": "Manager_ID",
                    "parentTable": "Employees", "parentColumn": "Employee_ID",
                    "parentNameColumn": "Name", "excelNameColumn": "Manager"
                }
            ]
        }
        res = MigrationEngine.execute_migration("upload_selfref_test")

    assert res.status == "COMPLETED"
    with engine.connect() as conn:
        emp = conn.execute(text("SELECT Employee_ID, Manager_ID, Name FROM Employees WHERE Name = 'Bob Developer'")).fetchone()
        assert emp is not None
        assert emp[1] == 1  # Alice Director's ID


# -----------------------------------------------------------------------------
# 8. Unresolved Foreign Key Handling & Transaction Rollback
# -----------------------------------------------------------------------------

def test_unresolved_foreign_key_causes_rollback():
    """
    Test that when a required foreign key cannot be resolved and has no default,
    the migration engine fails safely and rolls back all changes.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Clients (Client_ID INTEGER PRIMARY KEY AUTOINCREMENT, Client_Name TEXT)"))
        conn.execute(text("CREATE TABLE Projects (Project_ID INTEGER PRIMARY KEY AUTOINCREMENT, Client_ID INTEGER NOT NULL, Proj_Name TEXT)"))

    fk_client = ForeignKeyInfo(constrained_column="Client_ID", target_table="Clients", target_column="Client_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="ProjectsDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Clients", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Client_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Client_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Client_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Projects", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Project_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Client_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_client]),
                    ColumnMetadata(name="Proj_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Project_ID"], foreign_keys=[fk_client]
            )
        ]
    )

    # DataFrame with Projects only, no Client provided
    df = pd.DataFrame([
        {"Project": "Secret Alpha"}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Project", databaseTable="Projects", databaseColumn="Proj_Name", status="MATCHED")
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}

        try:
            res = MigrationEngine.execute_migration("upload_unresolved_fk_test")
            assert res.status in ("BLOCKED", "FAILED")
            assert res.transaction in ("NOT_EXECUTED", "ROLLED_BACK")
        except Exception:
            pass

    # Verify no partial rows were committed
    with engine.connect() as conn:
        projects_count = conn.execute(text("SELECT COUNT(*) FROM Projects")).scalar()
        assert projects_count == 0


# -----------------------------------------------------------------------------
# 9. Preview Engine Multi-level Parent GENERATED_ID Tagging
# -----------------------------------------------------------------------------

def test_preview_engine_marks_parent_keys_as_generated():
    """
    Test that the preview engine accurately previews GENERATED_ID for parent PKs
    across all active tables in topological order, performing zero writes.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Departments (Dept_ID INTEGER PRIMARY KEY AUTOINCREMENT, Dept_Name TEXT)"))
        conn.execute(text("CREATE TABLE Teams (Team_ID INTEGER PRIMARY KEY AUTOINCREMENT, Dept_ID INTEGER, Team_Name TEXT)"))

    fk_team_dept = ForeignKeyInfo(constrained_column="Dept_ID", target_table="Departments", target_column="Dept_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="OrgDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Departments", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Dept_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Dept_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Dept_ID"], foreign_keys=[]
            ),
            TableMetadata(
                name="Teams", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Team_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Dept_ID", type="INT", nullable=False, primary_key=False, foreign_keys=[fk_team_dept]),
                    ColumnMetadata(name="Team_Name", type="VARCHAR", nullable=False, primary_key=False)
                ],
                primary_keys=["Team_ID"], foreign_keys=[fk_team_dept]
            )
        ]
    )

    df = pd.DataFrame([
        {"Department": "R&D", "Team": "Robotics"}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="Department", databaseTable="Departments", databaseColumn="Dept_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Team", databaseTable="Teams", databaseColumn="Team_Name", status="MATCHED")
    ]

    preview_rows, dep_plan, planned_ops, val_summary, order, val_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="departments_teams.xlsx",
        schema=schema_metadata,
        column_mappings=mappings,
        engine=engine
    )

    assert len(preview_rows) > 0
    dept_id_preview = next(p for p in preview_rows if p.target_column == "Dept_ID")
    assert dept_id_preview.value_source in ("IDENTITY", "GENERATED_ID")
    assert dept_id_preview.final_sql_value in ("Generated by SQL Server", "<GENERATED_ID>")


    # Ensure zero database writes
    with engine.connect() as conn:
        d_cnt = conn.execute(text("SELECT COUNT(*) FROM Departments")).scalar()
        t_cnt = conn.execute(text("SELECT COUNT(*) FROM Teams")).scalar()
        assert d_cnt == 0 and t_cnt == 0

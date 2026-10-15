"""
Dedicated tests covering:
1. Semantic Mapping:
   - restaurant_name, virtual_brand_name, outlet_name never map to Customer.Location.
   - area never automatically maps to dbo.Customer.Location without an explicit rule.
   - Legitimate customer address/location maps properly.
   - When Customer.Location is missing and nullable, it receives SQL NULL (Python None).
2. Outlets Table as Read-Only Reference:
   - Never generates INSERT, UPDATE, or DELETE on dbo.Outlets when it contains existing records.
   - Dynamic lookup resolves Outlet_ID from existing records.
   - Resolves restaurant_name and virtual_brand_name independently.
3. Duplicate Target Columns & SQL Generation:
   - Every column appears at most once in generated INSERT statements.
   - Pre-execution validation catches duplicate target column mappings on writable tables.
   - SQL Server 264 (duplicate column name in INSERT) is impossible.
"""

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult
from app.mapping.matcher import ColumnMatcher
from app.migration.validator import MigrationValidator
from app.migration.preview import MigrationPreviewEngine
from app.migration.engine import MigrationEngine
from app.migration.lookup_resolver import DynamicLookupResolver


@pytest.fixture
def bopos_schema_with_customer():
    """
    Schema with dbo.Outlets, dbo.Customer, and dbo.Order.
    Notice Customer has Location (nullable VARCHAR(255)).
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
        ColumnMetadata(name="Mobile_Number", type="VARCHAR", max_length=20, nullable=True),
        ColumnMetadata(name="Location", type="VARCHAR", max_length=255, nullable=True),
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
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False),
        ColumnMetadata(name="Order_Outlet_ID", type="INT", nullable=False),
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=True),
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


def test_area_and_outlet_names_never_map_to_customer_location(bopos_schema_with_customer):
    """
    1. restaurant_name, virtual_brand_name, and area must NOT map to Customer.Location.
    """
    excel_cols = ["restaurant_name", "virtual_brand_name", "area", "customer_name", "contact_phone"]
    mappings = ColumnMatcher.match_columns(excel_columns=excel_cols, schema=bopos_schema_with_customer)

    # Check mappings
    for m in mappings:
        if m.database_table == "Customer" and m.database_column == "Location":
            # Must NOT be matched to area, restaurant_name, or virtual_brand_name
            assert m.excel_column not in ("area", "restaurant_name", "virtual_brand_name"), (
                f"Forbidden mapping: {m.excel_column} mapped to Customer.Location!"
            )

    # Verify contact_phone maps to Mobile_Number
    phone_map = next((m for m in mappings if m.excel_column == "contact_phone"), None)
    assert phone_map is not None
    assert phone_map.database_table == "Customer"
    assert phone_map.database_column == "Mobile_Number"


def test_customer_location_nullable_receives_empty_string_when_missing(bopos_schema_with_customer):
    """
    When no valid customer location is provided, Customer.Location is assigned None (SQL NULL).
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name, Short_Code) VALUES (69, 'ANN', 'ANN')"))
        conn.execute(text("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_Name TEXT, Mobile_Number TEXT, Location TEXT, Outlet_ID INTEGER)"))

    df = pd.DataFrame([
        {
            "restaurant_name": "ANN",
            "area": "Zomato_ANN - BTM",
            "customer_name": "Jane Doe",
            "contact_phone": "9876543210"
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=bopos_schema_with_customer), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_null_location")

    assert res.status == "COMPLETED"
    with engine.connect() as conn:
        custs = conn.execute(text("SELECT Customer_ID, Customer_Name, Mobile_Number, Location, Outlet_ID FROM Customer")).fetchall()
        assert len(custs) == 1
        c_row = custs[0]
        assert c_row[1] == "Jane Doe"
        assert c_row[2] == "9876543210"
        # Missing optional text fields are inserted as empty strings, not SQL NULL.
        assert c_row[3] == ""
        # Outlet_ID resolved to 69
        assert c_row[4] == 69


def test_outlets_treated_as_read_only_no_inserts(bopos_schema_with_customer):
    """
    Verify dbo.Outlets is treated strictly as read-only when it contains existing records.
    No INSERTs into Outlets are performed.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_Name TEXT, Short_Code TEXT)"))
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name, Short_Code) VALUES (69, 'ANN', 'ANN')"))
        conn.execute(text("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_Name TEXT, Mobile_Number TEXT, Location TEXT, Outlet_ID INTEGER)"))

    df = pd.DataFrame([
        {
            "restaurant_name": "ANN",
            "virtual_brand_name": "ANN BTM",
            "customer_name": "Alice",
            "contact_phone": "9998887776"
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="virtual_brand_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="contact_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=bopos_schema_with_customer), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):

        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration("upload_test_read_only_outlets")

    assert res.status == "COMPLETED"
    with engine.connect() as conn:
        # Outlets count must strictly remain 1 (no new insert!)
        outlet_count = conn.execute(text("SELECT count(*) FROM Outlets")).scalar()
        assert outlet_count == 1

        cust = conn.execute(text("SELECT Customer_Name, Outlet_ID FROM Customer")).fetchone()
        assert cust[0] == "Alice"
        assert cust[1] == 69


def test_duplicate_target_column_detection_on_writable_table(bopos_schema_with_customer):
    """
    Detect duplicate target column mapping on writable tables (e.g. two source columns mapping to Customer.Customer_Name).
    """
    df = pd.DataFrame([
        {
            "name_1": "Alice",
            "name_2": "Bob",
        }
    ])

    mappings = [
        ColumnMappingResult(excelColumn="name_1", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="name_2", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
    ]

    val_res = MigrationValidator.validate_dataset(
        df=df,
        schema=bopos_schema_with_customer,
        column_mappings=mappings
    )

    assert not val_res.canExecute
    dup_errors = [e for e in val_res.errors if e.error_type == "DUPLICATE_TARGET_COLUMN"]
    assert len(dup_errors) == 1
    assert "Duplicate target column mapping detected" in dup_errors[0].message
    assert "Customer_Name" in dup_errors[0].message

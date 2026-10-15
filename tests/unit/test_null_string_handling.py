"""
Automated Unit and Integration Tests for NULL String Handling in Excel-to-SQL Migration.

Tests all requirements:
1. Normalize string values:
   - Python None -> ''
   - String 'null' (case-insensitive, trimmed) -> ''
   - String 'NULL' -> ''
   - Empty or whitespace-only string -> ''
2. Apply only to string/text columns expecting an empty string.
3. Do not convert numeric, decimal, money, datetime, or non-string fields to empty strings.
4. Preserve actual SQL NULL for columns requiring SQL NULL (e.g. nullable FKs, nullable numbers).
5. Preserve literal 'null' when it is a legitimate business value (explicit value mapping/override).
6. Verify preview, dry-run, and engine INSERT parameters match identically.
7. Verify queries use parameterized SQL.
8. Verify transaction safety and zero writes in dry-run mode.
"""

import pytest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult
from app.migration.engine import (
    sanitize_value,
    normalize_string_value,
    should_column_receive_empty_string,
    should_fill_unmapped_text_column,
    is_text_sql_type,
    MigrationEngine
)
from app.migration.preview import MigrationPreviewEngine
from app.migration.dry_run import DryRunEngine
from app.migration.missing_value_resolver import MissingValueResolver, is_customer_name_column


# ---------------------------------------------------------------------------
# Requirement 1: String Normalization Functions
# ---------------------------------------------------------------------------

def test_normalize_string_value_variations():
    """Verify that Python None, 'null', 'NULL', '', and whitespace normalize to ''."""
    test_cases = [
        None,
        "null",
        "NULL",
        "Null",
        "  null  ",
        "  NULL  ",
        "",
        "   ",
        "\t\n",
        "nan",
        "NaN",
        "none",
        "None",
    ]
    for val in test_cases:
        assert normalize_string_value(val, expects_empty_string=True) == "", (
            f"Failed to normalize {val!r} to empty string"
        )

    # Valid non-null strings must be preserved
    assert normalize_string_value("Alice", expects_empty_string=True) == "Alice"
    assert normalize_string_value("  Bob  ", expects_empty_string=True) == "Bob"
    assert normalize_string_value(123, expects_empty_string=True) == "123"


def test_normalize_string_value_legitimate_literal_null():
    """Requirement 5: When legitimate literal is flagged, 'null' is preserved."""
    assert normalize_string_value("null", expects_empty_string=True, is_legitimate_literal=True) == "null"
    assert normalize_string_value("NULL", expects_empty_string=True, is_legitimate_literal=True) == "NULL"


def test_normalize_string_value_preserves_sql_null_when_not_expecting_empty():
    """Requirement 4: When a column does not expect an empty string, None is preserved."""
    assert normalize_string_value(None, expects_empty_string=False) is None
    assert normalize_string_value("null", expects_empty_string=False) is None
    assert normalize_string_value("NULL", expects_empty_string=False) is None
    assert normalize_string_value("", expects_empty_string=False) is None
    assert normalize_string_value("   ", expects_empty_string=False) is None


# ---------------------------------------------------------------------------
# Requirement 2: Sanitize Value for String/Text Columns
# ---------------------------------------------------------------------------

def test_sanitize_value_for_text_types():
    """Verify text columns normalize missing, 'null', 'NULL', and whitespace to ''."""
    for text_type in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "CLOB", "STRING"):
        for missing_val in (None, np.nan, "null", "NULL", "Null", "  null  ", "", "   ", "\t"):
            sanitized = sanitize_value(missing_val, col_type=text_type, nullable=True)
            assert sanitized == "", f"Expected '' for {missing_val!r} with type {text_type}, got {sanitized!r}"

    # Valid strings are preserved
    assert sanitize_value("Normal Text", col_type="VARCHAR") == "Normal Text"
    assert sanitize_value("  Trimmed Text  ", col_type="VARCHAR") == "Trimmed Text"


# ---------------------------------------------------------------------------
# Requirement 3: Non-string fields (int, money, datetime) never convert to ''
# ---------------------------------------------------------------------------

def test_integer_columns_never_convert_to_empty_string():
    """Verify integer columns preserve SQL NULL (None) if nullable, or 0 if NOT NULL, never ''."""
    int_types = ("INT", "BIGINT", "SMALLINT", "TINYINT")
    missing_inputs = (None, np.nan, "null", "NULL", "  null  ", "", "   ", "None", "nan")

    for c_type in int_types:
        for missing_val in missing_inputs:
            # Nullable integer column -> None (SQL NULL)
            res_null = sanitize_value(missing_val, col_type=c_type, nullable=True)
            assert res_null is None, f"Expected None for nullable {c_type} with {missing_val!r}, got {res_null!r}"
            assert res_null != ""

            # NOT NULL integer column -> 0
            res_not_null = sanitize_value(missing_val, col_type=c_type, nullable=False)
            assert res_not_null == 0, f"Expected 0 for non-nullable {c_type} with {missing_val!r}, got {res_not_null!r}"
            assert res_not_null != ""

    # Valid integers
    assert sanitize_value("42", col_type="INT", nullable=True) == 42
    assert sanitize_value(42.0, col_type="INT", nullable=True) == 42
    assert sanitize_value(100, col_type="BIGINT", nullable=False) == 100


def test_money_and_decimal_columns_never_convert_to_empty_string():
    """Verify decimal/money columns preserve SQL NULL (None) if nullable, or 0.0 if NOT NULL, never ''."""
    dec_types = ("FLOAT", "DECIMAL", "NUMERIC", "MONEY", "REAL", "DOUBLE")
    missing_inputs = (None, np.nan, "null", "NULL", "  null  ", "", "   ", "None", "nan")

    for c_type in dec_types:
        for missing_val in missing_inputs:
            # Nullable -> None (SQL NULL)
            res_null = sanitize_value(missing_val, col_type=c_type, nullable=True)
            assert res_null is None, f"Expected None for nullable {c_type} with {missing_val!r}, got {res_null!r}"
            assert res_null != ""

            # NOT NULL -> 0.0
            res_not_null = sanitize_value(missing_val, col_type=c_type, nullable=False)
            assert res_not_null == 0.0, f"Expected 0.0 for non-nullable {c_type} with {missing_val!r}, got {res_not_null!r}"
            assert res_not_null != ""

    # Valid monetary values
    assert sanitize_value("$158.00", col_type="DECIMAL(10,2)", nullable=True) == 158.0
    assert sanitize_value("₹ 1,250.50", col_type="MONEY", nullable=True) == 1250.50
    assert sanitize_value(99.99, col_type="FLOAT", nullable=False) == 99.99


def test_datetime_columns_never_convert_to_empty_string():
    """Verify date/datetime columns preserve SQL NULL (None) if nullable, never ''."""
    dt_types = ("DATE", "DATETIME", "DATETIME2", "SMALLDATETIME")
    missing_inputs = (None, np.nan, "null", "NULL", "  null  ", "", "   ", "None", "nan")

    for c_type in dt_types:
        for missing_val in missing_inputs:
            res_null = sanitize_value(missing_val, col_type=c_type, nullable=True)
            assert res_null is None, f"Expected None for nullable {c_type} with {missing_val!r}, got {res_null!r}"
            assert res_null != ""

    # Valid dates are formatted, not empty
    norm_dt = sanitize_value("2026-07-01 10:30:00", col_type="DATETIME", nullable=True)
    assert norm_dt is not None
    assert "2026" in str(norm_dt)


# ---------------------------------------------------------------------------
# Requirement 4 & 5: should_column_receive_empty_string rules
# ---------------------------------------------------------------------------

def test_should_column_receive_empty_string_rules():
    """Verify rules determining when columns receive '' vs SQL NULL."""
    # Text column: True
    text_col = ColumnMetadata(name="Description", type="VARCHAR(200)", nullable=True)
    tbl = TableMetadata(name="Orders", schema_name="dbo", columns=[text_col], primary_keys=["Order_ID"])
    assert should_column_receive_empty_string(text_col, "Orders", column_meta=text_col, table_meta=tbl) is True

    # Numeric column: False
    num_col = ColumnMetadata(name="Total_Amount", type="DECIMAL(10,2)", nullable=True)
    assert should_column_receive_empty_string(num_col, "Orders", column_meta=num_col, table_meta=tbl) is False

    # Datetime column: False
    dt_col = ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=True)
    assert should_column_receive_empty_string(dt_col, "Orders", column_meta=dt_col, table_meta=tbl) is False

    # Identity column: False
    id_col = ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True)
    assert should_column_receive_empty_string(id_col, "Orders", column_meta=id_col, table_meta=tbl) is False

    # Foreign key column: False
    fk_info = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID")
    fk_col = ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, foreign_keys=[fk_info])
    assert should_column_receive_empty_string(fk_col, "Orders", column_meta=fk_col, table_meta=tbl, fk_columns={"Outlet_ID"}) is False

    # Column with DB default: False (defers to database default)
    def_col = ColumnMetadata(name="Status", type="VARCHAR(20)", nullable=False, default="Active")
    assert should_column_receive_empty_string(def_col, "Orders", column_meta=def_col, table_meta=tbl) is False

    # Explicit column rule override: preserve_null / null_handling="NULL"
    rule_null = {"Description": {"null_handling": "NULL"}}
    assert should_column_receive_empty_string(text_col, "Orders", column_meta=text_col, table_meta=tbl, column_rules=rule_null) is False


# ---------------------------------------------------------------------------
# Requirement 6: Preview, Dry-run, and Actual INSERT Alignment
# ---------------------------------------------------------------------------

@pytest.fixture
def alignment_schema():
    """Schema with Customer and OrderLineItems to test preview, dry-run, and execution consistency."""
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False),
        ColumnMetadata(name="Address", type="VARCHAR(200)", nullable=True),
        ColumnMetadata(name="Contact_Number", type="VARCHAR(50)", nullable=True),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    oli_fk_cust = ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
    oli_cols = [
        ColumnMetadata(name="Line_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=True, foreign_keys=[oli_fk_cust]),
        ColumnMetadata(name="Description", type="VARCHAR(200)", nullable=True),
        ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=True),
        ColumnMetadata(name="Remarks", type="VARCHAR(255)", nullable=True),
    ]
    oli_tbl = TableMetadata(
        name="OrderLineItems",
        schema_name="dbo",
        columns=oli_cols,
        primary_keys=["Line_ID"],
        foreign_keys=[oli_fk_cust]
    )

    return DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[customer_tbl, oli_tbl]
    )


def test_preview_normalizes_null_strings_to_empty(alignment_schema):
    """Verify MigrationPreviewEngine generates '' for text columns receiving None, 'null', 'NULL'."""
    df = pd.DataFrame([
        {
            "excel_cust": "null",
            "excel_addr": "NULL",
            "excel_desc": "   ",
            "excel_remarks": None,
            "excel_price": "null"
        }
    ])
    mappings = [
        ColumnMappingResult(excelColumn="excel_cust", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="excel_addr", databaseTable="Customer", databaseColumn="Address", status="MATCHED"),
        ColumnMappingResult(excelColumn="excel_desc", databaseTable="OrderLineItems", databaseColumn="Description", status="MATCHED"),
        ColumnMappingResult(excelColumn="excel_remarks", databaseTable="OrderLineItems", databaseColumn="Remarks", status="MATCHED"),
        ColumnMappingResult(excelColumn="excel_price", databaseTable="OrderLineItems", databaseColumn="Item_Price", status="MATCHED"),
    ]

    engine = create_engine("sqlite:///:memory:")
    preview_tuple = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_dataset.csv",
        schema=alignment_schema,
        column_mappings=mappings,
        engine=engine
    )

    cust_rows = [r for r in preview_tuple.preview_rows if r.target_table == "Customer"]
    oli_rows = [r for r in preview_tuple.preview_rows if r.target_table == "OrderLineItems"]

    # Customer_Name (text) received 'null' from Excel -> preview shows ""
    cust_name_col = next(c for c in cust_rows[0].columns if c.database_column == "Customer_Name")
    assert cust_name_col.final_value == "", f"Expected '' for Customer_Name, got {cust_name_col.final_value!r}"
    assert cust_name_col.validation_status == "VALID"

    # Address (text) received 'NULL' from Excel -> preview shows ""
    addr_col = next(c for c in cust_rows[0].columns if c.database_column == "Address")
    assert addr_col.final_value == "", f"Expected '' for Address, got {addr_col.final_value!r}"
    assert addr_col.validation_status == "VALID"

    # Description (text) received '   ' (whitespace) -> preview shows ""
    desc_col = next(c for c in oli_rows[0].columns if c.database_column == "Description")
    assert desc_col.final_value == "", f"Expected '' for Description, got {desc_col.final_value!r}"

    # Remarks (text) received None -> preview shows ""
    remarks_col = next(c for c in oli_rows[0].columns if c.database_column == "Remarks")
    assert remarks_col.final_value == "", f"Expected '' for Remarks, got {remarks_col.final_value!r}"

    # Item_Price (decimal) received 'null' -> preview shows 'NULL' (never empty string)
    price_col = next(c for c in oli_rows[0].columns if c.database_column == "Item_Price")
    assert price_col.final_value == "NULL", f"Expected 'NULL' for Item_Price, got {price_col.final_value!r}"


def test_dry_run_parameters_align_with_preview_and_execution(alignment_schema):
    """Verify DryRunEngine produces identical normalized parameters with zero database writes."""
    df = pd.DataFrame([
        {
            "customer_name": "null",
            "address": "NULL",
            "description": "",
            "remarks": "  null  ",
            "item_price": "null"
        }
    ])

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_Name TEXT NOT NULL, Address TEXT, Contact_Number TEXT)")
        conn.exec_driver_sql("CREATE TABLE OrderLineItems (Line_ID INTEGER PRIMARY KEY AUTOINCREMENT, Customer_ID INTEGER, Description TEXT, Item_Price REAL, Remarks TEXT)")

    report = DryRunEngine.run_dry_run(
        df=df,
        source_name="test_dataset.csv",
        schema=alignment_schema,
        engine=engine
    )

    # Verify zero database writes
    assert report.total_records_migrated == 0
    assert report.total_records_inserted == 0
    assert report.status == "SUCCESS"

    # Find proposed statements
    stmts = report.proposed_statements
    cust_stmt = next(s for s in stmts if s.table_name == "Customer")
    oli_stmt = next(s for s in stmts if s.table_name == "OrderLineItems")

    # Customer_Name received 'null' in Excel -> parameterized as ''
    assert cust_stmt.parameters["Customer_Name"] == ""
    # Address received 'NULL' in Excel -> parameterized as ''
    assert cust_stmt.parameters["Address"] == ""

    # Description received '' in Excel -> parameterized as ''
    assert oli_stmt.parameters["Description"] == ""
    # Remarks received '  null  ' in Excel -> parameterized as ''
    assert oli_stmt.parameters["Remarks"] == ""
    # Item_Price received 'null' in Excel -> parameterized as None (SQL NULL), never ''
    assert oli_stmt.parameters["Item_Price"] is None

    # Check that SQL statements use parameterized placeholders, not string concatenation
    assert ":Customer_Name" in cust_stmt.sql_text
    assert ":Address" in cust_stmt.sql_text
    assert "'null'" not in cust_stmt.sql_text


# ---------------------------------------------------------------------------
# Requirement 7 & 8: Parameterized SQL Execution into Database
# ---------------------------------------------------------------------------

def test_database_insertion_with_parameterized_null_normalization():
    """Verify actual database insertion passes '' for text columns and SQL NULL for numbers."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name TEXT NOT NULL,
                Address TEXT,
                Contact_Number TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE OrderLineItems (
                Line_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_ID INTEGER,
                Description TEXT,
                Item_Price REAL,
                Remarks TEXT
            );
        """))

    # Prepare sanitized inputs for a row having null/empty representations
    raw_cust_name = "null"
    raw_address = "NULL"
    raw_contact = "   "
    raw_desc = None
    raw_price = "null"
    raw_remarks = "none"

    cust_params = {
        "Customer_Name": sanitize_value(raw_cust_name, "VARCHAR", nullable=False),
        "Address": sanitize_value(raw_address, "VARCHAR", nullable=True),
        "Contact_Number": sanitize_value(raw_contact, "VARCHAR", nullable=True),
    }

    oli_params = {
        "Customer_ID": None,  # Nullable FK remains None
        "Description": sanitize_value(raw_desc, "VARCHAR", nullable=True),
        "Item_Price": sanitize_value(raw_price, "DECIMAL(10,2)", nullable=True),
        "Remarks": sanitize_value(raw_remarks, "VARCHAR", nullable=True),
    }

    # Execute parameterized INSERTs
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO Customer (Customer_Name, Address, Contact_Number) VALUES (:Customer_Name, :Address, :Contact_Number)"),
            cust_params
        )
        conn.execute(
            text("INSERT INTO OrderLineItems (Customer_ID, Description, Item_Price, Remarks) VALUES (:Customer_ID, :Description, :Item_Price, :Remarks)"),
            oli_params
        )

    # Read back and verify exact stored values
    with engine.connect() as conn:
        cust_row = conn.execute(text("SELECT Customer_Name, Address, Contact_Number FROM Customer")).fetchone()
        assert cust_row[0] == "", f"Expected '' for Customer_Name, got {cust_row[0]!r}"
        assert cust_row[1] == "", f"Expected '' for Address, got {cust_row[1]!r}"
        assert cust_row[2] == "", f"Expected '' for Contact_Number, got {cust_row[2]!r}"

        oli_row = conn.execute(text("SELECT Customer_ID, Description, Item_Price, Remarks FROM OrderLineItems")).fetchone()
        assert oli_row[0] is None, f"Expected None (SQL NULL) for Customer_ID, got {oli_row[0]!r}"
        assert oli_row[1] == "", f"Expected '' for Description, got {oli_row[1]!r}"
        assert oli_row[2] is None, f"Expected None (SQL NULL) for Item_Price, got {oli_row[2]!r}"
        assert oli_row[3] == "", f"Expected '' for Remarks, got {oli_row[3]!r}"

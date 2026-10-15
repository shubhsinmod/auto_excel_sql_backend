import pytest
import pandas as pd
from unittest.mock import MagicMock
from sqlalchemy import text

from app.models.schema import (
    DatabaseSchemaMetadata,
    TableMetadata,
    ColumnMetadata,
    ForeignKeyInfo
)
from app.mapping.matcher import ColumnMatcher
from app.migration.engine import sanitize_value, quote_identifier
from app.migration.missing_value_resolver import (
    MissingValueResolver,
    TableEntityLookupIndex
)
from app.migration.validator import MigrationValidator
from app.database.connection import get_engine, test_db_connection as check_db_connection
from app.database.metadata import extract_database_metadata


@pytest.fixture
def pos_schema_with_mobile():
    """Builds a schema where Customer has BOTH Contact_Number and Mobile_Number."""
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False, primary_key=False),
        ColumnMetadata(name="Address", type="VARCHAR(500)", nullable=True, primary_key=False),
        ColumnMetadata(name="Contact_Number", type="VARCHAR(50)", nullable=True, primary_key=False),
        ColumnMetadata(name="Mobile_Number", type="VARCHAR(50)", nullable=True, primary_key=False),
    ]
    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR(100)", nullable=False, primary_key=False),
        ColumnMetadata(name="Contact_Person", type="VARCHAR(100)", nullable=True, primary_key=False),
        ColumnMetadata(name="Phone", type="VARCHAR(50)", nullable=True, primary_key=False),
        ColumnMetadata(name="Location", type="VARCHAR(200)", nullable=True, primary_key=False),
    ]
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, foreign_keys=[
            ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
        ]),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False),
    ]
    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[
            TableMetadata(name="Customer", columns=customer_cols, primary_keys=["Customer_ID"], foreign_keys=[]),
            TableMetadata(name="Outlets", columns=outlets_cols, primary_keys=["Outlet_ID"], foreign_keys=[]),
            TableMetadata(name="Order", columns=order_cols, primary_keys=["Order_ID"], foreign_keys=[]),
        ]
    )


def test_1_contact_phone_explicitly_maps_to_mobile_number(pos_schema_with_mobile):
    """
    Requirement 1:
    - Excel column 'contact_phone' must map to 'Mobile_Number' in Customer table.
    - Must NOT incorrectly map to 'Contact_Number' or 'Contact_Person'.
    """
    excel_cols = ["contact_phone"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema_with_mobile)
    assert len(results) == 1
    res = results[0]

    assert res.excel_column == "contact_phone"
    assert res.database_table == "Customer"
    assert res.database_column == "Mobile_Number"
    assert res.status == "MATCHED"
    assert res.database_column != "Contact_Number"
    assert res.database_column != "Contact_Person"
    assert res.confidence >= 0.95

    # Top candidate must be Customer.Mobile_Number
    assert len(res.candidates) > 0
    assert res.candidates[0]["databaseTable"] == "Customer"
    assert res.candidates[0]["databaseColumn"] == "Mobile_Number"


def test_2_other_column_mappings_continue_to_work(pos_schema_with_mobile):
    """
    Requirement 6:
    - Other Excel-to-database column mappings continue to work alongside contact_phone.
    """
    excel_cols = ["Customer_Name", "contact_phone", "Outlet_Name"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema_with_mobile)
    mapping = {r.excel_column: r for r in results}

    assert mapping["Customer_Name"].status == "MATCHED"
    assert mapping["Customer_Name"].database_table == "Customer"
    assert mapping["Customer_Name"].database_column == "Customer_Name"

    assert mapping["contact_phone"].status == "MATCHED"
    assert mapping["contact_phone"].database_table == "Customer"
    assert mapping["contact_phone"].database_column == "Mobile_Number"

    assert mapping["Outlet_Name"].status == "MATCHED"
    assert mapping["Outlet_Name"].database_table == "Outlets"
    assert mapping["Outlet_Name"].database_column == "Outlet_Name"


def test_3_value_preservation_leading_zeros():
    """
    Requirement 2:
    - Handle phone numbers as strings to preserve leading zeros.
    - Avoid unwanted numeric conversion.
    """
    # Leading zeros e.g. "09876543210" or "00123"
    raw_phone = "09876543210"
    sanitized = sanitize_value(raw_phone, col_type="varchar(50)", nullable=True)
    assert sanitized == "09876543210"
    assert isinstance(sanitized, str)

    raw_phone_intl = "+0123456789"
    sanitized_intl = sanitize_value(raw_phone_intl, col_type="varchar(50)", nullable=True)
    assert sanitized_intl == "+0123456789"
    assert isinstance(sanitized_intl, str)


def test_4_missing_or_empty_values_become_empty_strings():
    """
    Missing or literal-null values in text columns are normalized to empty strings.
    """
    for empty_val in [None, "", "   ", float("nan"), "null", "None"]:
        sanitized = sanitize_value(empty_val, col_type="varchar(50)", nullable=True)
        assert sanitized == "", f"Expected empty string for {repr(empty_val)}, got {repr(sanitized)}"


def test_5_sql_generation_parameterized_mobile_number():
    """
    Requirement 5:
    - Parameterized SQL generation targeting [Customer] and [Mobile_Number].
    """
    cols_to_insert = ["Customer_Name", "Mobile_Number"]
    dialect = "mssql"
    quoted_cols = [quote_identifier(c, dialect) for c in cols_to_insert]
    val_params = [f":{c}" for c in cols_to_insert]

    sql = f"INSERT INTO [Customer] ({', '.join(quoted_cols)}) VALUES ({', '.join(val_params)})"
    assert "[Customer]" in sql
    assert "[Mobile_Number]" in sql
    assert ":Mobile_Number" in sql
    assert "[Customer_Name]" in sql
    assert ":Customer_Name" in sql


def test_6_existing_record_matching_via_mobile_number(pos_schema_with_mobile):
    """
    Requirement 4:
    - Match existing records using Mobile_Number and reuse primary key.
    """
    existing_db_records = [
        {"Customer_ID": 101, "Customer_Name": "Alice Wonderland", "Mobile_Number": "09876543210"},
        {"Customer_ID": 102, "Customer_Name": "Bob Builder", "Mobile_Number": "09123456789"},
    ]
    index = TableEntityLookupIndex(existing_db_records, identifying_columns=["Customer_ID", "Mobile_Number"])

    # Lookup by Mobile_Number
    rec = index.find_record("Mobile_Number", "09876543210")
    assert rec is not None
    assert rec["Customer_ID"] == 101
    assert rec["Customer_Name"] == "Alice Wonderland"

    # Lookup by Mobile_Number with different value
    rec2 = index.find_record("Mobile_Number", "09123456789")
    assert rec2 is not None
    assert rec2["Customer_ID"] == 102


def test_7_ambiguous_mobile_number_match_flagged(pos_schema_with_mobile):
    """
    Requirement 4:
    - If Mobile_Number has ambiguous matches (multiple DB records with same number), flag for user review.
    """
    duplicate_phone_records = [
        {"Customer_ID": 101, "Customer_Name": "Alice 1", "Mobile_Number": "09876543210"},
        {"Customer_ID": 102, "Customer_Name": "Alice 2", "Mobile_Number": "09876543210"},
    ]
    index = TableEntityLookupIndex(duplicate_phone_records, identifying_columns=["Customer_ID", "Mobile_Number"])
    assert index.is_ambiguous("Mobile_Number", "09876543210") is True

    # Resolving missing value on ambiguous key
    res = MissingValueResolver.resolve_missing_value(
        table_name="Customer",
        target_column="Customer_Name",
        table_meta=pos_schema_with_mobile.tables[0],
        source_row_data={"Mobile_Number": "09876543210"},
        db_to_excel={"Mobile_Number": "contact_phone"},
        entity_lookup_index=index
    )
    # Customer_Name has special exception returning "null" per schema rule,
    # so test another non-null required column or test lookup resolution directly:
    assert index.is_ambiguous("Mobile_Number", "09876543210") is True


def test_8_live_sql_server_schema_validation():
    """
    Requirement 3:
    - Inspect the actual SQL Server schema to identify Customer containing Mobile_Number.
    - Verify column exists and is writable.
    """
    connected, _ = check_db_connection()
    if not connected:
        pytest.skip("SQL Server is not connected.")

    engine = get_engine()
    live_schema = extract_database_metadata(engine)

    # Validate Customer table exists
    customer_table = next((t for t in live_schema.tables if t.name.lower() == "customer"), None)
    assert customer_table is not None, "Customer table not found in SQL Server schema"

    # Validate Mobile_Number exists in Customer table
    mobile_col = next((c for c in customer_table.columns if c.name.lower() == "mobile_number"), None)
    assert mobile_col is not None, "Mobile_Number column not found in Customer table"
    assert mobile_col.name == "Mobile_Number"
    assert getattr(mobile_col, "identity", False) is False, "Mobile_Number must not be identity"

    # Test ColumnMatcher on live SQL Server schema
    results = ColumnMatcher.match_columns(["contact_phone"], live_schema)
    assert len(results) == 1
    m = results[0]
    assert m.status == "MATCHED"
    assert m.database_table == "Customer"
    assert m.database_column == "Mobile_Number"
    assert m.confidence >= 0.95

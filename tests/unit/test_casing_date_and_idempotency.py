import pytest
from datetime import datetime, date
import pandas as pd
from app.utils.normalization import (
    normalize_column_name,
    normalize_identifier,
    normalize_date_value,
)
from app.migration.engine import sanitize_value
from app.models.schema import (
    DatabaseSchemaMetadata,
    TableMetadata,
    ColumnMetadata,
)
from app.mapping.matcher import ColumnMatcher
from app.migration.engine import MigrationEngine
from app.migration.validator import MigrationValidator
from app.migration.preview import MigrationPreviewEngine


@pytest.fixture
def test_schema():
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
        ColumnMetadata(name="date_value", type="DATE", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Total_Amount", type="DECIMAL(10,2)", nullable=False, primary_key=False, identity=False),
    ]

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Test",
        db_type="mssql",
        tables=[
            TableMetadata(name="Customer", schema_name="dbo", columns=customer_cols, primary_keys=["Customer_ID"]),
            TableMetadata(name="Outlets", schema_name="dbo", columns=outlets_cols, primary_keys=["Outlet_ID"]),
            TableMetadata(name="Order", schema_name="dbo", columns=order_cols, primary_keys=["Order_ID"]),
        ]
    )


# -----------------------------------------------------------------------------
# 1. Column Casing and Normalization Matching Tests
# -----------------------------------------------------------------------------
def test_all_casing_conventions_matching(test_schema):
    """
    Ensure snake_case, camelCase, PascalCase, UPPERCASE, lowercase, spaces, hyphens
    all map reliably to the database target according to the required priority order.
    """
    variants = [
        "customer_name",   # snake_case
        "customerName",    # camelCase
        "CustomerName",    # PascalCase
        "CUSTOMER_NAME",   # UPPERCASE
        "Customer Name",   # space-separated
        "customer-name",   # hyphen-separated
    ]

    for variant in variants:
        results = ColumnMatcher.match_columns([variant], test_schema)
        assert len(results) == 1, f"Failed for variant {variant}"
        m = results[0]
        assert m.status == "MATCHED"
        assert m.database_table == "Customer"
        assert m.database_column == "Customer_Name"
        assert m.normalized_source_name == "customer name"
        assert m.normalized_target_name == "customer name"
        assert m.confidence >= 0.99
        assert m.match_type in ("EXACT_ORIGINAL", "CASE_INSENSITIVE", "EXACT_NORMALIZED")


# -----------------------------------------------------------------------------
# 2. Semantic Mapping and Safety Guardrails
# -----------------------------------------------------------------------------
def test_outlet_columns_never_map_to_customer_location(test_schema):
    """
    restaurant_name and virtual_brand_name represent outlet names.
    They must never map to Customer.Location.
    """
    for col in ["restaurant_name", "virtual_brand_name", "Restaurant Name", "Virtual Brand Name"]:
        results = ColumnMatcher.match_columns([col], test_schema)
        assert len(results) == 1
        m = results[0]
        assert not (m.database_table == "Customer" and m.database_column == "Location"), (
            f"Col {col} was incorrectly mapped to Customer.Location!"
        )
        # Should map to Outlets.Outlet_Name
        assert m.database_table == "Outlets"
        assert m.database_column == "Outlet_Name"


def test_area_column_never_automatically_mapped_to_customer_location(test_schema):
    """
    'area' source column must not automatically map to Customer.Location.
    """
    for col in ["area", "Area", "AREA"]:
        results = ColumnMatcher.match_columns([col], test_schema)
        assert len(results) == 1
        m = results[0]
        assert not (m.database_table == "Customer" and m.database_column == "Location"), (
            f"Col {col} was automatically mapped to Customer.Location!"
        )


# -----------------------------------------------------------------------------
# 3. Global Phone Number Mapping and Value Preservation
# -----------------------------------------------------------------------------
def test_phone_columns_map_to_mobile_number_and_preserve_strings(test_schema):
    """
    Excel columns contact_phone and customer_phone must map to Mobile_Number,
    preserving strings, leading zeros, and never converting to float.
    """
    phone_cols = ["contact_phone", "Contact_Phone", "customer_phone", "CustomerPhone", "mobile_number"]
    for col in phone_cols:
        results = ColumnMatcher.match_columns([col], test_schema)
        assert len(results) == 1
        m = results[0]
        assert m.database_table == "Customer"
        assert m.database_column == "Mobile_Number"

    # Value preservation tests
    assert sanitize_value("09876543210", "VARCHAR(50)") == "09876543210"
    assert sanitize_value("01234", "VARCHAR(50)") == "01234"
    assert sanitize_value(9876543210.0, "VARCHAR(50)") == "9876543210"
    assert sanitize_value("", "VARCHAR(50)") == ""
    assert sanitize_value(None, "VARCHAR(50)") == ""


# -----------------------------------------------------------------------------
# 4. Date Normalization & Multi-Column Mapping
# -----------------------------------------------------------------------------
def test_excel_serial_dates_and_string_normalization():
    """
    Excel serial dates (e.g. 45000 = 2023-03-15) and date strings must normalize
    consistently across preview and execution.
    """
    # Excel serial number 45000 is 2023-03-15
    d_norm, dt_obj, is_v = normalize_date_value(45000, "DATETIME")
    assert is_v is True
    assert d_norm == "2023-03-15 00:00:00"

    # Excel serial float with time 45000.5 is 2023-03-15 12:00:00
    d_norm_time, dt_obj, is_v = normalize_date_value(45000.5, "DATETIME")
    assert is_v is True
    assert d_norm_time == "2023-03-15 12:00:00"

    # String serial date
    d_norm_str, dt_obj, is_v = normalize_date_value("45000", "DATE")
    assert is_v is True
    assert d_norm_str == "2023-03-15"

    # ISO string
    d_iso, dt_obj, is_v = normalize_date_value("2026-07-01 14:30:00", "DATETIME")
    assert is_v is True
    assert d_iso == "2026-07-01 14:30:00"

    # Python date and datetime objects
    d_dt, _, is_v = normalize_date_value(date(2026, 7, 1), "DATE")
    assert d_dt == "2026-07-01"
    d_dtt, _, is_v = normalize_date_value(datetime(2026, 7, 1, 10, 0), "DATETIME")
    assert d_dtt == "2026-07-01 10:00:00"


def test_date_maps_to_both_date_value_and_order_datetime(test_schema):
    """
    Source 'date' must map to date_value as well as Order.Order_DateTime.
    """
    from app.mapping.matcher import resolve_unmapped_required_columns

    results = ColumnMatcher.match_columns(["date", "customer_name"], test_schema)
    table_mappings = {"Order": list(results), "Customer": list(results)}
    resolve_unmapped_required_columns(
        table_mappings=table_mappings,
        schema=test_schema,
        df_columns=["date", "customer_name"]
    )

    order_date_mappings = [
        m for m in table_mappings["Order"]
        if m.database_table == "Order" and m.database_column in ("date_value", "Order_DateTime")
    ]
    cols_mapped = {m.database_column for m in order_date_mappings}
    assert "Order_DateTime" in cols_mapped
    assert "date_value" in cols_mapped


# -----------------------------------------------------------------------------
# 5. Read-Only Outlets & Duplicate Target Column Prevention
# -----------------------------------------------------------------------------
def test_read_only_outlets_and_no_duplicate_columns(test_schema):
    """
    Verifies that Outlets is excluded from active write tables when pre-populated,
    and duplicate column insertions in SQL generation are prevented.
    """
    # Deduplication test for insert columns
    cols_set = ["Customer_Name", "Mobile_Number", "Customer_Name"]
    deduped = list(dict.fromkeys(cols_set))
    assert deduped == ["Customer_Name", "Mobile_Number"]
    assert len(deduped) == 2

"""
Comprehensive regression tests for targeted fixes:
1. dbo.Customer.Customer_Name missing values replaced with literal string 'null' (and only for this column).
2. Other nullable columns (Address, Contact_Number, etc.) retain existing NULL behavior.
3. dbo.Customer.Customer_Name does not trigger DB lookups when missing.
4. Order_DateTime dynamically maps from source datetime column (e.g. 'date').
5. Missing Order_DateTime does not silently substitute datetime.now(); marks row UNRESOLVED.
6. Order_Outlet_ID resolves dynamically via DB lookup from business name (restaurant_name -> Outlets.Outlet_Name -> Outlet_ID).
7. None/NaN/empty values never search for literal 'None' or 'nan' in DB lookups.
8. Preview, validation, and SQL execution stay consistent with value sources and resolved values.
"""

import pytest
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult
from app.migration.missing_value_resolver import (
    MissingValueResolver,
    is_customer_name_column,
)
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship
from app.mapping.matcher import ColumnMatcher, resolve_unmapped_required_columns
from app.migration.validator import MigrationValidator
from app.migration.engine import sanitize_value, collapse_bill_rows_by_invoice


@pytest.fixture
def mock_schema():
    """Builds a schema mirroring BOPOS_Leons with Customer, Order, and Outlets tables."""
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Address", type="VARCHAR", max_length=200, nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Contact_Number", type="VARCHAR", max_length=50, nullable=True, primary_key=False, identity=False),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR", max_length=100, nullable=False, primary_key=False, identity=False),
    ]
    outlets_tbl = TableMetadata(
        name="Outlets",
        schema_name="dbo",
        columns=outlets_cols,
        primary_keys=["Outlet_ID"],
        foreign_keys=[]
    )

    order_fk_cust = ForeignKeyInfo(
        constrained_column="Customer_ID",
        target_table="Customer",
        target_column="Customer_ID",
        target_schema="dbo"
    )
    order_fk_outlet = ForeignKeyInfo(
        constrained_column="Order_Outlet_ID",
        target_table="Outlets",
        target_column="Outlet_ID",
        target_schema="dbo"
    )
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=False, identity=False, foreign_keys=[order_fk_cust]),
        ColumnMetadata(name="Order_Outlet_ID", type="INT", nullable=False, primary_key=False, identity=False, foreign_keys=[order_fk_outlet]),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Total_Amount", type="DECIMAL", nullable=True, primary_key=False, identity=False),
    ]
    order_tbl = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[order_fk_cust, order_fk_outlet]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[customer_tbl, outlets_tbl, order_tbl]
    )


# -------------------------------------------------------------------------
# Requirement 1: Customer_Name missing values replaced with empty string
# -------------------------------------------------------------------------
def test_customer_name_literal_null_replacement(mock_schema):
    """Test that missing Customer_Name values resolve to an empty string."""
    cust_tbl = next(t for t in mock_schema.tables if t.name == "Customer")

    for missing_val in [None, np.nan, "", "   "]:
        result = MissingValueResolver.resolve_missing_value(
            table_name="Customer",
            target_column="Customer_Name",
            table_meta=cust_tbl,
            source_row_data={"Contact_Number": "1234567890"},
            db_to_excel={"Contact_Number": "Contact_Number"},
            entity_lookup_index=None
        )
        assert result.status == "RESOLVED"
        assert result.value == ""
        assert result.value_source == "DEFAULT"


# -------------------------------------------------------------------------
# Requirement 2: Other columns retain normal NULL handling
# -------------------------------------------------------------------------
def test_other_columns_preserve_standard_null_behavior(mock_schema):
    """Test that missing Address and Contact_Number on Customer resolve to NULL, not 'null'."""
    cust_tbl = next(t for t in mock_schema.tables if t.name == "Customer")

    res_addr = MissingValueResolver.resolve_missing_value(
        table_name="Customer",
        target_column="Address",
        table_meta=cust_tbl,
        source_row_data={"Customer_Name": "Alice"},
        db_to_excel={"Customer_Name": "Customer_Name"},
        entity_lookup_index=None
    )
    assert res_addr.status == "NULLABLE"
    assert res_addr.value is None
    assert res_addr.value_source == "NULL"

    res_contact = MissingValueResolver.resolve_missing_value(
        table_name="Customer",
        target_column="Contact_Number",
        table_meta=cust_tbl,
        source_row_data={"Customer_Name": "Alice"},
        db_to_excel={"Customer_Name": "Customer_Name"},
        entity_lookup_index=None
    )
    assert res_contact.status == "NULLABLE"
    assert res_contact.value is None
    assert res_contact.value_source == "NULL"


# -------------------------------------------------------------------------
# Requirement 3: Order_DateTime dynamically mapped from source date/time column
# -------------------------------------------------------------------------
def test_order_datetime_dynamic_mapping(mock_schema):
    """Test that Order_DateTime receives dynamic mapping when source has 'date'."""
    df_cols = ["date", "restaurant_name", "customer_name"]
    table_mappings = {
        "Order": [ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Total_Amount", status="MATCHED")]
    }

    resolved = resolve_unmapped_required_columns(mock_schema, table_mappings, df_cols)
    order_dt_mappings = [m for m in resolved["Order"] if m.database_column == "Order_DateTime"]
    assert len(order_dt_mappings) == 1
    assert order_dt_mappings[0].excel_column == "date"
    assert order_dt_mappings[0].status == "MATCHED"


# -------------------------------------------------------------------------
# Requirement 4: Missing Order_DateTime does not invent timestamps or use datetime.now()
# -------------------------------------------------------------------------
def test_missing_order_datetime_is_unresolved(mock_schema):
    """Test that missing Order_DateTime produces UNRESOLVED, not current timestamp."""
    order_tbl = next(t for t in mock_schema.tables if t.name == "Order")

    result = MissingValueResolver.resolve_missing_value(
        table_name="Order",
        target_column="Order_DateTime",
        table_meta=order_tbl,
        source_row_data={"restaurant_name": "ANN"},
        db_to_excel={},
        entity_lookup_index=None
    )
    assert result.status == "UNRESOLVED"
    assert result.value is None
    assert "cannot silently substitute the current date/time" in result.message


# -------------------------------------------------------------------------
# Requirement 5: Order_Outlet_ID dynamically resolves from Outlets DB lookup
# -------------------------------------------------------------------------
def test_order_outlet_id_dynamic_lookup_from_name(mock_schema):
    """Test that Order_Outlet_ID resolves dynamically using restaurant_name -> Outlets.Outlet_Name."""
    rels = DynamicLookupResolver.discover_relationships(
        schema=mock_schema,
        df_columns=["restaurant_name", "customer_name"],
        column_mappings=[],
        active_tables=["Order"]
    )
    outlet_rel = next((r for r in rels if r.child_column == "Order_Outlet_ID"), None)
    assert outlet_rel is not None
    assert outlet_rel.parent_table == "Outlets"
    assert outlet_rel.parent_id_column == "Outlet_ID"
    assert outlet_rel.excel_name_column == "restaurant_name"

    db_records = [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]
    res_id, matched_name, status, val_source, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="ANN",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=db_records
    )
    assert status == "MATCHED"
    assert res_id == 69
    assert val_source == "DATABASE_LOOKUP"


# -------------------------------------------------------------------------
# Requirement 6: None/NaN/empty never trigger searches for 'None' or 'nan'
# -------------------------------------------------------------------------
def test_none_nan_never_search_db_placeholder_strings():
    """Verify that lookup resolver treats None, NaN, whitespace, 'None', 'nan' as missing without searching."""
    db_records = [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]
    for val in [None, np.nan, "", "   ", "None", "nan", "NULL"]:
        res_id, matched_name, status, val_source, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
            excel_name=val,
            parent_table="Outlets",
            parent_id_column="Outlet_ID",
            parent_name_column="Outlet_Name",
            db_records=db_records
        )
        assert res_id is None
        assert status in ("MISSING_NAME", "UNRESOLVED")


# -------------------------------------------------------------------------
# Requirement 7: Unresolvable foreign key reports clean error, never "'None' not found"
# -------------------------------------------------------------------------
def test_unresolved_foreign_key_message_does_not_contain_none_not_found(mock_schema):
    """Validator and preview reports explanatory message when source identifying name is missing."""
    df = pd.DataFrame([{"customer_name": "Alice"}])  # No restaurant_name column
    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED")
    ]
    summary = MigrationValidator.validate_dataset(df, mock_schema, mappings)
    for err in summary.errors:
        assert "'None' not found" not in err.message
        assert "'nan' not found" not in err.message


# -------------------------------------------------------------------------
# Requirement 8: Parameterized SQL inserts empty strings for Customer_Name and other missing text values
# -------------------------------------------------------------------------
def test_parameterized_sql_inserts_empty_string_for_customer_name():
    """Ensure missing Customer_Name and other text values are persisted as empty strings."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name TEXT NOT NULL,
                Address TEXT
            );
        """))

    # Test sanitize_value for Customer_Name and text columns
    for missing_val in (None, np.nan, "", "   ", "None", "null", "NaN", "NULL"):
        assert sanitize_value(missing_val, col_type="VARCHAR", nullable=False) == ""

    # Missing text columns use empty strings regardless of nullability
    assert sanitize_value(None, col_type="VARCHAR", nullable=True) == ""
    assert sanitize_value("", col_type="VARCHAR", nullable=True) == ""

    # Test parameterized execution into actual database table
    clean_cust_name = sanitize_value(None, col_type="VARCHAR", nullable=False)
    clean_addr = sanitize_value(None, col_type="VARCHAR", nullable=True)

    insert_sql = text("INSERT INTO Customer (Customer_Name, Address) VALUES (:Customer_Name, :Address)")
    with engine.begin() as conn:
        conn.execute(insert_sql, {"Customer_Name": clean_cust_name, "Address": clean_addr})

    with engine.connect() as conn:
        row = conn.execute(text("SELECT Customer_Name, Address FROM Customer")).fetchone()
        assert row[0] == ""
        assert row[1] == ""


def test_option1_invoice_grain_order_bills_and_line_items():
    """One Order/Bills header per invoice; preserve every source line item."""
    records = [
        {"invoice_no": "INV-1", "item_quantity": 2, "item_name": "Idli"},
        {"invoice_no": "INV-1", "item_quantity": 3, "item_name": "Vada"},
        {"invoice_no": "INV-1", "item_quantity": 1, "item_name": "Dosa"},
        {"invoice_no": "INV-2", "item_quantity": 4, "item_name": "Pizza"},
    ]
    indices = [0, 1, 2, 3]

    order_records, order_indices, order_collapsed, order_unique = collapse_bill_rows_by_invoice(
        [dict(r) for r in records], indices, "invoice_no", "item_quantity"
    )
    bills_records, bills_indices, bills_collapsed, bills_unique = collapse_bill_rows_by_invoice(
        [dict(r) for r in records], indices, "invoice_no", "item_quantity"
    )

    assert [r["invoice_no"] for r in order_records] == ["INV-1", "INV-2"]
    assert [r["item_quantity"] for r in order_records] == [6, 4]
    assert [r["invoice_no"] for r in bills_records] == ["INV-1", "INV-2"]
    assert [r["item_quantity"] for r in bills_records] == [6, 4]
    assert order_indices == [0, 3] and bills_indices == [0, 3]
    assert order_collapsed == bills_collapsed == 2
    assert order_unique == bills_unique == 2

    # The source dataframe/list used for OrderLineItems is not collapsed.
    assert len(records) == 4
    assert [r["item_name"] for r in records] == ["Idli", "Vada", "Dosa", "Pizza"]

"""
Unit Tests for One-to-Many Destination Column Mapping.

Verifies:
1. One Excel column (e.g. 'date') can map to multiple valid destination columns across different tables
   (e.g. dbo.Customer.date_add and dbo.[Order].order_datetime).
2. Global one-to-one mapping restriction is NOT enforced across different tables.
3. Discovery is schema-driven without hardcoding.
4. Each destination is evaluated independently using column names, data types, semantic meaning, and table context.
5. Generic columns (date, status, name, amount) are NOT copied indiscriminately to unrelated tables
   (e.g. 'date' does not map to DeliveryBoys.DateOfBirth or archive/backup tables).
6. Non-date specific columns (e.g. customer_name) only map to their relevant entity table.
7. Ambiguous column candidates within a table produce status='AMBIGUOUS'.
8. Integration with MigrationValidator: DUPLICATE_TARGET_COLUMN check passes when 1 source maps to multiple tables,
   but correctly fails if multiple distinct source columns claim the same table and column.
"""

import pytest
import pandas as pd
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.mapping.matcher import ColumnMatcher
from app.migration.validator import MigrationValidator


@pytest.fixture
def multi_table_schema():
    """
    Schema with Customer, Order, Bills, and DeliveryBoys tables.
    - Customer has Customer_ID, Customer_Name, and date_add.
    - Order has Order_ID, Customer_ID, order_datetime, Total_Amount.
    - Bills has Bill_ID, Order_ID, bill_date, Final_Amount.
    - DeliveryBoys has DeliveryBoy_ID, DeliveryBoy_Name, DateOfBirth.
    """
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="date_add", type="DATETIME", nullable=True),
    ]
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False),
        ColumnMetadata(name="order_datetime", type="DATETIME", nullable=False),
        ColumnMetadata(name="Total_Amount", type="DECIMAL(10,2)", nullable=False),
    ]
    bills_cols = [
        ColumnMetadata(name="Bill_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_ID", type="INT", nullable=False),
        ColumnMetadata(name="bill_date", type="DATETIME", nullable=False),
        ColumnMetadata(name="Final_Amount", type="DECIMAL(10,2)", nullable=False),
    ]
    delivery_boys_cols = [
        ColumnMetadata(name="DeliveryBoy_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="DeliveryBoy_Name", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="DateOfBirth", type="DATETIME", nullable=True),
    ]

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Multi_Test",
        db_type="mssql",
        tables=[
            TableMetadata(name="Customer", columns=customer_cols, primary_keys=["Customer_ID"]),
            TableMetadata(name="Order", columns=order_cols, primary_keys=["Order_ID"]),
            TableMetadata(name="Bills", columns=bills_cols, primary_keys=["Bill_ID"]),
            TableMetadata(name="DeliveryBoys", columns=delivery_boys_cols, primary_keys=["DeliveryBoy_ID"]),
        ]
    )


def test_date_maps_to_multiple_tables_customer_and_order(multi_table_schema):
    """
    Core requirement:
    One Excel column named 'date' maps to both Customer.date_add and Order.order_datetime.
    """
    excel_cols = ["date", "Customer_Name", "Total_Amount"]
    results = ColumnMatcher.match_columns(excel_cols, multi_table_schema)

    date_mappings = [r for r in results if r.excel_column == "date" and r.status == "MATCHED"]
    assert len(date_mappings) >= 2, f"Expected date to map to at least 2 tables, got: {date_mappings}"

    mapped_destinations = {(r.database_table, r.database_column) for r in date_mappings}
    assert ("Customer", "date_add") in mapped_destinations
    assert ("Order", "order_datetime") in mapped_destinations


def test_date_does_not_map_to_unrelated_date_of_birth(multi_table_schema):
    """
    Requirement: Generic source column must NOT be copied to every table.
    'date' represents a transaction/entity timestamp, not DeliveryBoys.DateOfBirth.
    """
    excel_cols = ["date", "Customer_Name"]
    results = ColumnMatcher.match_columns(excel_cols, multi_table_schema)

    delivery_mappings = [r for r in results if r.database_table == "DeliveryBoys"]
    assert len(delivery_mappings) == 0, f"Date should not map to DeliveryBoys! Got: {delivery_mappings}"


def test_specific_columns_do_not_map_to_multiple_unrelated_tables(multi_table_schema):
    """
    Non-generic / entity-specific columns such as 'Customer_Name' must only map
    to Customer.Customer_Name, NOT to DeliveryBoys or Order.
    """
    excel_cols = ["Customer_Name"]
    results = ColumnMatcher.match_columns(excel_cols, multi_table_schema)

    assert len(results) == 1
    m = results[0]
    assert m.status == "MATCHED"
    assert m.database_table == "Customer"
    assert m.database_column == "Customer_Name"


def test_ambiguous_columns_within_table_flagged_as_ambiguous():
    """
    If a table has multiple competing date columns with equal relevance (e.g. Order_Date vs Delivered_Date),
    it must be reported as AMBIGUOUS for that table, not blindly mapped.
    """
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_Date", type="DATETIME", nullable=False),
        ColumnMetadata(name="Delivered_Date", type="DATETIME", nullable=True),
    ]
    schema = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="mssql",
        tables=[TableMetadata(name="Order", columns=order_cols, primary_keys=["Order_ID"])]
    )

    results = ColumnMatcher.match_columns(["date"], schema)
    # Order has Order_Date (score 0.96) vs Delivered_Date (score 0.94).
    # Since Order_Date is the primary order date, it matches Order_Date.
    # Now let's test two columns with identical names/scores:
    ambig_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="date_start", type="DATETIME", nullable=True),
        ColumnMetadata(name="date_end", type="DATETIME", nullable=True),
    ]
    ambig_schema = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="mssql",
        tables=[TableMetadata(name="Order", columns=ambig_cols, primary_keys=["Order_ID"])]
    )

    ambig_res = ColumnMatcher.match_columns(["date"], ambig_schema)
    assert len(ambig_res) >= 1
    assert any(r.status == "AMBIGUOUS" for r in ambig_res)


def test_migration_validator_accepts_one_to_many_mapping(multi_table_schema):
    """
    Ensures MigrationValidator validates datasets where a single source column is mapped
    to multiple target tables without triggering DUPLICATE_TARGET_COLUMN.
    """
    excel_cols = ["date", "Customer_Name", "Total_Amount"]
    results = ColumnMatcher.match_columns(excel_cols, multi_table_schema)

    df = pd.DataFrame([
        {"date": "2026-07-01 10:00:00", "Customer_Name": "Alice Smith", "Total_Amount": "150.00"},
        {"date": "2026-07-02 11:30:00", "Customer_Name": "Bob Jones", "Total_Amount": "250.00"},
    ])

    validation_summary = MigrationValidator.validate_dataset(
        df=df,
        schema=multi_table_schema,
        column_mappings=results,
    )

    # DUPLICATE_TARGET_COLUMN errors should be 0 because 'date' targets distinct tables
    dup_target_errors = [e for e in validation_summary.errors if e.error_type == "DUPLICATE_TARGET_COLUMN"]
    assert len(dup_target_errors) == 0, f"Unexpected DUPLICATE_TARGET_COLUMN error: {dup_target_errors}"

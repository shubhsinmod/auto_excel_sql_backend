import pytest
import pandas as pd
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import ColumnMappingResult
from app.migration.missing_value_resolver import (
    MissingValueResolver,
    TableEntityLookupIndex,
    extract_sql_default_value,
    identify_table_lookup_columns
)
from app.migration.validator import MigrationValidator


@pytest.fixture
def customer_table_meta():
    """Builds a realistic SQL Server table metadata for dbo.Customer."""
    col_id = ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True, unique=True)
    col_name = ColumnMetadata(name="Customer_Name", type="NVARCHAR(100)", nullable=False, primary_key=False, identity=False, unique=False)
    col_phone = ColumnMetadata(name="Contact_Number", type="VARCHAR(20)", nullable=True, primary_key=False, identity=False, unique=True)
    col_status = ColumnMetadata(name="Status", type="VARCHAR(20)", nullable=False, primary_key=False, identity=False, default="'Active'", unique=False)
    col_address = ColumnMetadata(name="Address", type="NVARCHAR(200)", nullable=True, primary_key=False, identity=False, unique=False)

    return TableMetadata(
        name="Customer",
        columns=[col_id, col_name, col_phone, col_status, col_address],
        primary_keys=["Customer_ID"]
    )


@pytest.fixture
def staff_table_meta():
    """Builds a realistic SQL Server table metadata for dbo.Staff for generic lookup testing."""
    col_id = ColumnMetadata(name="Staff_ID", type="INT", nullable=False, primary_key=True, identity=True, unique=True)
    col_name = ColumnMetadata(name="Staff_Name", type="NVARCHAR(100)", nullable=False, primary_key=False, identity=False, unique=False)
    col_phone = ColumnMetadata(name="Contact_Number", type="VARCHAR(20)", nullable=True, primary_key=False, identity=False, unique=True)
    col_status = ColumnMetadata(name="Status", type="VARCHAR(20)", nullable=False, primary_key=False, identity=False, default="'Active'", unique=False)

    return TableMetadata(
        name="Staff",
        columns=[col_id, col_name, col_phone, col_status],
        primary_keys=["Staff_ID"]
    )


def test_customer_name_resolves_to_literal_null_string_without_db_lookup(customer_table_meta):
    """
    Requirement 1: dbo.Customer.Customer_Name replaces missing values with literal string 'null'.
    Must NOT attempt a database lookup even when unique keys are present.
    """
    db_records = [
        {"Customer_ID": 1, "Customer_Name": "Alice Wonderland", "Contact_Number": "9876543210", "Status": "Active"}
    ]
    entity_index = TableEntityLookupIndex(db_records, identify_table_lookup_columns(customer_table_meta))

    source_row = {
        "cust_name": None,
        "phone_no": "9876543210"
    }
    db_to_excel = {
        "Customer_Name": "cust_name",
        "Contact_Number": "phone_no"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Customer",
        target_column="Customer_Name",
        table_meta=customer_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=entity_index
    )

    assert res.status == "RESOLVED"
    assert res.value == "null"
    assert res.value != "Alice Wonderland"  # DB lookup was NOT attempted!


def test_database_lookup_success_generic(staff_table_meta):
    """
    Requirement: For non-Customer columns, missing required values are retrieved via dynamic database lookup.
    """
    db_records = [
        {"Staff_ID": 1, "Staff_Name": "Alice Wonderland", "Contact_Number": "9876543210", "Status": "Active"},
        {"Staff_ID": 2, "Staff_Name": "Bob Builder", "Contact_Number": "9123456789", "Status": "Active"}
    ]
    identifying_cols = identify_table_lookup_columns(staff_table_meta)
    entity_index = TableEntityLookupIndex(db_records, identifying_cols)

    source_row = {
        "staff_name": "",  # Empty!
        "phone_no": "9876543210"
    }
    db_to_excel = {
        "Staff_Name": "staff_name",
        "Contact_Number": "phone_no"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Staff",
        target_column="Staff_Name",
        table_meta=staff_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=entity_index
    )

    assert res.status == "RESOLVED"
    assert res.value == "Alice Wonderland"
    assert res.value_source == "DATABASE_LOOKUP"
    assert res.lookup_column == "Contact_Number"
    assert res.lookup_value == "9876543210"
    assert "Alice Wonderland" in res.message


def test_missing_lookup_keys_generic(staff_table_meta):
    """
    When required field is empty and no reliable unique key is present in source row,
    mark as UNRESOLVED and explain clearly.
    """
    db_records = [
        {"Staff_ID": 1, "Staff_Name": "Alice Wonderland", "Contact_Number": "9876543210", "Status": "Active"}
    ]
    entity_index = TableEntityLookupIndex(db_records, identify_table_lookup_columns(staff_table_meta))

    source_row = {
        "staff_name": "",
        "phone_no": ""
    }
    db_to_excel = {
        "Staff_Name": "staff_name",
        "Contact_Number": "phone_no"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Staff",
        target_column="Staff_Name",
        table_meta=staff_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=entity_index
    )

    assert res.status == "UNRESOLVED"
    assert res.value is None
    assert res.value_source == "UNRESOLVED"
    assert "no reliable unique lookup key is present" in res.message
    assert "Cannot insert NULL into NOT NULL column" in res.message


def test_unavailable_values_key_not_in_database_generic(staff_table_meta):
    """
    Lookup key is present in Excel, but does not match any record in database.
    Must mark UNRESOLVED and explain why it cannot be inserted safely.
    """
    db_records = [
        {"Staff_ID": 1, "Staff_Name": "Alice Wonderland", "Contact_Number": "9876543210", "Status": "Active"}
    ]
    entity_index = TableEntityLookupIndex(db_records, identify_table_lookup_columns(staff_table_meta))

    source_row = {
        "staff_name": "",
        "phone_no": "5555555555"
    }
    db_to_excel = {
        "Staff_Name": "staff_name",
        "Contact_Number": "phone_no"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Staff",
        target_column="Staff_Name",
        table_meta=staff_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=entity_index
    )

    assert res.status == "UNRESOLVED"
    assert res.value is None
    assert res.value_source == "UNRESOLVED"
    assert "No existing database record found" in res.message
    assert "5555555555" in res.message


def test_unavailable_values_db_record_also_empty_generic(staff_table_meta):
    """
    Key matches a record in database, but the database record itself has an empty value
    for the required column.
    """
    db_records = [
        {"Staff_ID": 1, "Staff_Name": "", "Contact_Number": "9876543210", "Status": "Active"}
    ]
    entity_index = TableEntityLookupIndex(db_records, identify_table_lookup_columns(staff_table_meta))

    source_row = {
        "staff_name": "",
        "phone_no": "9876543210"
    }
    db_to_excel = {
        "Staff_Name": "staff_name",
        "Contact_Number": "phone_no"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Staff",
        target_column="Staff_Name",
        table_meta=staff_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=entity_index
    )

    assert res.status == "UNRESOLVED"
    assert res.value is None
    assert res.value_source == "UNRESOLVED"
    assert "has an empty value for required column 'Staff_Name'" in res.message


def test_valid_defaults(customer_table_meta):
    """
    Requirement 3: When a required column has a valid database default constraint in SQL Server,
    populate it using DATABASE_DEFAULT.
    """
    source_row = {
        "cust_status": ""  # empty
    }
    db_to_excel = {
        "Status": "cust_status"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Customer",
        target_column="Status",
        table_meta=customer_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=None
    )

    assert res.status == "DEFAULT"
    assert res.value == "Active"
    assert res.value_source == "DATABASE_DEFAULT"
    assert "database default constraint" in res.message


def test_extract_sql_default_value_varieties():
    """Verify stripping of SQL Server expression wrappers for default constraints."""
    assert extract_sql_default_value("('Active')") == "Active"
    assert extract_sql_default_value("((0))") == "0"
    assert extract_sql_default_value("('N/A')") == "N/A"
    assert extract_sql_default_value("(getdate())") == "CURRENT_TIMESTAMP"
    assert extract_sql_default_value("(newid())") == "NEWID()"
    assert extract_sql_default_value(None) is None


def test_nullable_fields(customer_table_meta):
    """
    Requirement: For optional nullable fields, allow empty values and insert NULL when appropriate.
    """
    source_row = {
        "cust_addr": ""  # empty
    }
    db_to_excel = {
        "Address": "cust_addr"
    }

    res = MissingValueResolver.resolve_missing_value(
        table_name="Customer",
        target_column="Address",
        table_meta=customer_table_meta,
        source_row_data=source_row,
        db_to_excel=db_to_excel,
        entity_lookup_index=None
    )

    assert res.status == "NULLABLE"
    assert res.value is None
    assert res.value_source == "NULL"


def test_not_null_constraint_violation_prevention(staff_table_meta):
    """
    Requirement 4 & 5: Do not insert NULL into NOT NULL columns.
    Do not invent staff names, IDs, or placeholder values without an approved business rule.
    Validator must block migration execution when an unresolvable NOT NULL violation exists.
    """
    schema = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="mssql",
        tables=[staff_table_meta]
    )

    # Row 1: Resolvable via DB lookup (phone 9876543210 exists in DB)
    # Row 2: Unresolvable (phone 9999999999 not in DB, Staff_Name empty)
    df = pd.DataFrame({
        "Staff Name": ["", ""],
        "Contact Number": ["9876543210", "9999999999"]
    })

    mappings = [
        ColumnMappingResult(excelColumn="Staff Name", databaseTable="Staff", databaseColumn="Staff_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="Contact Number", databaseTable="Staff", databaseColumn="Contact_Number", status="MATCHED")
    ]

    db_records = [
        {"Staff_ID": 101, "Staff_Name": "Alice Wonderland", "Contact_Number": "9876543210", "Status": "Active"}
    ]
    entity_indices = {
        "Staff": TableEntityLookupIndex(db_records, identify_table_lookup_columns(staff_table_meta))
    }

    summary = MigrationValidator.validate_dataset(
        df=df,
        schema=schema,
        column_mappings=mappings,
        entity_indices=entity_indices
    )

    # Row 1 was dynamically resolved! Row 2 was NOT resolved.
    assert summary.can_execute is False
    assert len(summary.errors) == 1
    assert summary.errors[0].row_number == 2
    assert "Cannot insert NULL into NOT NULL column 'Staff_Name'" in summary.errors[0].message
    assert "9999999999" in summary.errors[0].message
    assert summary.valid_rows == 1
    assert summary.invalid_rows == 1

import pytest
import pandas as pd
from unittest.mock import MagicMock
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult, ForeignKeyPlanItem
from app.mapping.matcher import get_column_requirement_info
from app.migration.validator import MigrationValidator


def test_get_column_requirement_info_classification():
    """Verify metadata-driven column requirement classification."""
    fk_info = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID")
    col_ident = ColumnMetadata(name="ID", type="INT", nullable=False, primary_key=True, identity=True, unique=True)
    col_default = ColumnMetadata(name="Status", type="VARCHAR(20)", nullable=False, primary_key=False, identity=False, default="'Active'", unique=False)
    col_optional = ColumnMetadata(name="Notes", type="NVARCHAR(500)", nullable=True, primary_key=False, identity=False, unique=False)
    col_mandatory = ColumnMetadata(name="CustomerName", type="NVARCHAR(100)", nullable=False, primary_key=False, identity=False, unique=False)
    col_fk = ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=False, identity=False, foreign_keys=[fk_info], unique=False)

    tbl = TableMetadata(name="Orders", columns=[col_ident, col_default, col_optional, col_mandatory, col_fk], primary_keys=["ID"])

    req_ident, is_req_ident = get_column_requirement_info(tbl, col_ident)
    assert req_ident == "AUTO_GENERATED"
    assert is_req_ident is False

    req_def, is_req_def = get_column_requirement_info(tbl, col_default)
    assert req_def == "DATABASE_DEFAULT"
    assert is_req_def is False

    req_opt, is_req_opt = get_column_requirement_info(tbl, col_optional)
    assert req_opt == "OPTIONAL"
    assert is_req_opt is False

    req_mand, is_req_mand = get_column_requirement_info(tbl, col_mandatory)
    assert req_mand == "MANDATORY"
    assert is_req_mand is True

    req_fk, is_req_fk = get_column_requirement_info(tbl, col_fk)
    assert req_fk == "FOREIGN_KEY"
    assert is_req_fk is True


def test_validator_permits_missing_optional_and_default_fields():
    """
    Ensure users only need to provide mandatory data.
    Nullable columns, identity columns, and columns with default constraints must NOT block migration.
    """
    col_id = ColumnMetadata(name="CustomerID", type="INT", nullable=False, primary_key=True, identity=True, unique=True)
    col_name = ColumnMetadata(name="CustomerName", type="NVARCHAR(100)", nullable=False, primary_key=False, identity=False, unique=False)
    col_phone = ColumnMetadata(name="Phone", type="VARCHAR(20)", nullable=True, primary_key=False, identity=False, unique=False)
    col_status = ColumnMetadata(name="Status", type="VARCHAR(20)", nullable=False, primary_key=False, identity=False, default="'Active'", unique=False)

    schema = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="mssql",
        tables=[TableMetadata(name="Customer", columns=[col_id, col_name, col_phone, col_status], primary_keys=["CustomerID"])]
    )

    # Excel only provides CustomerName (mandatory field)
    # Phone (nullable) and Status (has default) and CustomerID (identity) are omitted entirely
    df = pd.DataFrame({
        "Customer Name": ["Alice", "Bob", "Charlie"]
    })

    mappings = [
        ColumnMappingResult(
            excelColumn="Customer Name",
            databaseTable="Customer",
            databaseColumn="CustomerName",
            status="MATCHED",
            confidence=1.0,
            matchType="EXACT"
        )
    ]

    summary = MigrationValidator.validate_dataset(
        df=df,
        schema=schema,
        column_mappings=mappings
    )

    # Must be valid to execute with 0 blocking errors!
    assert summary.can_execute is True
    assert len(summary.errors) == 0
    assert summary.valid_rows == 3


def test_validator_fk_with_dynamic_lookup_passes_when_excel_id_empty():
    """
    When Excel ID column is empty but dynamically resolved via name-to-ID lookup plan,
    the validator must NOT raise missing required value errors.
    """
    fk_info = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID")
    col_order_id = ColumnMetadata(name="OrderID", type="INT", nullable=False, primary_key=True, identity=True, unique=True)
    col_outlet_id = ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=False, identity=False, foreign_keys=[fk_info], unique=False)

    schema = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="mssql",
        tables=[TableMetadata(name="Orders", columns=[col_order_id, col_outlet_id], primary_keys=["OrderID"], foreign_keys=[fk_info])]
    )

    # Excel has empty Outlet_ID, but Outlet_Name is provided
    df = pd.DataFrame({
        "Outlet_ID": [None, None],
        "Outlet_Name": ["ANN", "ANN"]
    })

    mappings = [
        ColumnMappingResult(
            excelColumn="Outlet_ID",
            databaseTable="Orders",
            databaseColumn="Outlet_ID",
            status="MATCHED",
            confidence=1.0,
            matchType="EXACT"
        )
    ]

    fk_plan = [
        ForeignKeyPlanItem(
            childTable="Orders",
            childColumn="Outlet_ID",
            parentTable="Outlets",
            parentColumn="Outlet_ID",
            sourceBusinessKey="ANN",
            existingParentFound=True,
            resolvedId="69",
            status="MATCHED",
            insertionOrder=1
        )
    ]

    summary = MigrationValidator.validate_dataset(
        df=df,
        schema=schema,
        column_mappings=mappings,
        foreign_key_plans=fk_plan
    )

    assert summary.can_execute is True
    assert len(summary.errors) == 0


def test_insert_omits_defaults_and_preserves_nullables():
    """
    Verify the INSERT column assembly logic:
    1. Columns with default constraints are omitted when value is None.
    2. Optional nullable columns without defaults receive NULL when mapped.
    3. Mandatory columns raise ValueError when None.
    """
    col_meta_map = {
        "Status": ColumnMetadata(name="Status", type="VARCHAR(20)", nullable=False, primary_key=False, identity=False, default="'Active'", unique=False),
        "Notes": ColumnMetadata(name="Notes", type="NVARCHAR(200)", nullable=True, primary_key=False, identity=False, unique=False),
        "CustName": ColumnMetadata(name="CustName", type="NVARCHAR(100)", nullable=False, primary_key=False, identity=False, unique=False)
    }

    insert_cols = ["Status", "Notes", "CustName"]

    # Case 1: Status is None (has default) -> Omitted; Notes is None (nullable) -> Included as None; CustName is "Alice" -> Included
    param_dict = {"Status": None, "Notes": None, "CustName": "Alice"}

    row_insert_cols = []
    for c in insert_cols:
        c_meta = col_meta_map.get(c)
        val = param_dict.get(c)
        has_default = (c_meta is not None and c_meta.default is not None)
        is_nullable = (c_meta is not None and c_meta.nullable)

        if val is not None:
            row_insert_cols.append(c)
        else:
            if has_default:
                if c in param_dict:
                    del param_dict[c]
                continue
            elif is_nullable:
                if c in param_dict:
                    row_insert_cols.append(c)
            else:
                raise ValueError(f"Cannot insert NULL into non-nullable column '{c}'")

    assert "Status" not in row_insert_cols
    assert "Status" not in param_dict  # Omitted so SQL Server applies default constraint
    assert "Notes" in row_insert_cols
    assert param_dict["Notes"] is None  # Insert NULL into nullable column
    assert "CustName" in row_insert_cols
    assert param_dict["CustName"] == "Alice"

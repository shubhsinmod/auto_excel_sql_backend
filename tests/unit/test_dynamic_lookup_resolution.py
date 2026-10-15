import pytest
import pandas as pd
from sqlalchemy import create_engine, text

from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata, ForeignKeyInfo
from app.models.migration import (
    ColumnMappingResult,
    SourceRowPreviewItem,
    ForeignKeyPlanItem,
    ValueResolutionResult
)
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship
from app.migration.validator import MigrationValidator
from app.migration.preview import MigrationPreviewEngine


# -------------------------------------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------------------------------------

@pytest.fixture
def mock_schema():
    """
    Schema with:
    - Outlets (Outlet_ID INT PK IDENTITY, Outlet_Name VARCHAR(100) NOT NULL)
    - Customer (Customer_ID INT PK IDENTITY, Customer_Name VARCHAR(100) NOT NULL, Phone VARCHAR(20))
    - Order (Order_ID INT PK IDENTITY, Outlet_ID INT NOT NULL FK, Customer_ID INT NULL FK, Total DECIMAL)
    """
    col_out_id = ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True)
    col_out_name = ColumnMetadata(name="Outlet_Name", type="VARCHAR", nullable=False, primary_key=False, max_length=100)
    tbl_outlets = TableMetadata(
        name="Outlets",
        schema_name="dbo",
        columns=[col_out_id, col_out_name],
        primary_keys=["Outlet_ID"],
        foreign_keys=[]
    )

    col_cust_id = ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True)
    col_cust_name = ColumnMetadata(name="Customer_Name", type="VARCHAR", nullable=False, primary_key=False, max_length=100)
    tbl_customer = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=[col_cust_id, col_cust_name],
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    col_ord_id = ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True)
    col_ord_out_id = ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=False)
    col_ord_cust_id = ColumnMetadata(name="Customer_ID", type="INT", nullable=True, primary_key=False)
    col_ord_total = ColumnMetadata(name="Total", type="DECIMAL", nullable=True, primary_key=False)

    tbl_order = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=[col_ord_id, col_ord_out_id, col_ord_cust_id, col_ord_total],
        primary_keys=["Order_ID"],
        foreign_keys=[
            ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID"),
            ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
        ]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[tbl_outlets, tbl_customer, tbl_order]
    )


# -------------------------------------------------------------------------------------------------
# TEST 1: Successful Name-to-ID Resolution (Outlet_Name = ANN -> Outlet_ID = 69)
# -------------------------------------------------------------------------------------------------
def test_1_successful_name_to_id_resolution():
    """
    Excel input: Outlet_Name = 'ANN', Outlet_ID = empty
    Database: Outlets has Outlet_ID = 69, Outlet_Name = 'ANN'
    Expected:
    - Resolves Outlet_ID = 69 dynamically from database records.
    - Value source = 'DATABASE_LOOKUP'
    - Status = 'MATCHED'
    - Confidence = 1.0
    - Non-hardcoded.
    """
    db_records = [
        {"Outlet_ID": 69, "Outlet_Name": "ANN"},
        {"Outlet_ID": 70, "Outlet_Name": "Koramangala"}
    ]

    res_id, matched_name, status, val_source, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="ANN",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=db_records
    )

    assert status == "MATCHED"
    assert val_source == "DATABASE_LOOKUP"
    assert res_id == 69
    assert matched_name == "ANN"
    assert conf == 1.0
    assert len(candidates) == 1
    assert candidates[0]["id"] == 69


# -------------------------------------------------------------------------------------------------
# TEST 2: Normalization (Whitespace, Casing, Trim)
# -------------------------------------------------------------------------------------------------
def test_2_case_and_whitespace_normalization():
    """
    Excel input: '  ann  ', 'ann', 'Ann'
    Database: Outlets has 'ANN' with ID 69
    Expected:
    - Normalizes case and whitespace.
    - Resolves to Outlet_ID = 69, value source = 'DATABASE_LOOKUP'.
    """
    db_records = [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]

    for input_name in ["  ann  ", "ann", "Ann", "  ANN "]:
        res_id, matched_name, status, val_source, conf, candidates, _ = DynamicLookupResolver.match_name_to_id(
            excel_name=input_name,
            parent_table="Outlets",
            parent_id_column="Outlet_ID",
            parent_name_column="Outlet_Name",
            db_records=db_records
        )
        assert status == "MATCHED"
        assert res_id == 69
        assert matched_name == "ANN"
        assert val_source == "DATABASE_LOOKUP"


# -------------------------------------------------------------------------------------------------
# TEST 3: Missing Match (Unresolved - Do Not Invent IDs)
# -------------------------------------------------------------------------------------------------
def test_3_missing_match_unresolved():
    """
    Excel input: 'NonExistent Outlet'
    Database: Outlets has only ID 69 ('ANN')
    Expected:
    - Status = 'UNRESOLVED'
    - Resolved ID = None (never invents an arbitrary ID like 1 or 69)
    - Value source = 'UNRESOLVED'
    """
    db_records = [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]

    res_id, matched_name, status, val_source, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="NonExistent Outlet",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=db_records
    )

    assert status == "UNRESOLVED"
    assert res_id is None
    assert matched_name is None
    assert val_source == "UNRESOLVED"
    assert "not found" in msg.lower()


# -------------------------------------------------------------------------------------------------
# TEST 4: Duplicate Names in Database (Ambiguous - Do Not Pick Arbitrarily)
# -------------------------------------------------------------------------------------------------
def test_4_duplicate_names_ambiguous_review():
    """
    Excel input: 'Downtown'
    Database: Two outlets both named 'Downtown' with IDs 101 and 102.
    Expected:
    - Status = 'AMBIGUOUS'
    - Resolved ID = None (does NOT select the first record arbitrarily)
    - Candidates list contains both matching records with IDs and names for user selection.
    """
    db_records = [
        {"Outlet_ID": 101, "Outlet_Name": "Downtown"},
        {"Outlet_ID": 102, "Outlet_Name": "Downtown"}
    ]

    res_id, matched_name, status, val_source, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="Downtown",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=db_records
    )

    assert status == "AMBIGUOUS"
    assert res_id is None
    assert matched_name is None
    assert val_source == "AMBIGUOUS"
    assert len(candidates) == 2
    assert {c["id"] for c in candidates} == {101, 102}
    assert "user review required" in msg.lower()


# -------------------------------------------------------------------------------------------------
# TEST 5: Missing Identifying Name in Excel
# -------------------------------------------------------------------------------------------------
def test_5_missing_identifying_name():
    """
    Excel input: empty string, None, 'nan'
    Expected:
    - Status = 'MISSING_NAME'
    - Resolved ID = None
    - Value source = 'NULL'
    """
    db_records = [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]

    for empty_val in [None, "", "   ", "nan", "None", "NULL"]:
        res_id, matched_name, status, val_source, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
            excel_name=empty_val,
            parent_table="Outlets",
            parent_id_column="Outlet_ID",
            parent_name_column="Outlet_Name",
            db_records=db_records
        )
        assert status == "MISSING_NAME"
        assert res_id is None
        assert val_source == "NULL"


# -------------------------------------------------------------------------------------------------
# TEST 6: Validation Blocks Execution on Unresolved Required Foreign Key
# -------------------------------------------------------------------------------------------------
def test_6_validation_blocks_unresolved_required_fk(mock_schema):
    """
    When Order.Outlet_ID is a required foreign key (nullable=False),
    an unresolved or ambiguous lookup must trigger validation errors and set can_execute = False.
    """
    df = pd.DataFrame({
        "Outlet_Name": ["Unknown Outlet", "ANN"],
        "Total": [150.0, 200.0]
    })

    mappings = [
        ColumnMappingResult(excelColumn="Outlet_Name", databaseTable="Order", databaseColumn="Outlet_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="Total", databaseTable="Order", databaseColumn="Total", status="MATCHED")
    ]

    # ForeignKeyPlanItem with NOT_FOUND on required Outlet_ID
    fk_plan = [
        ForeignKeyPlanItem(
            childTable="Order",
            childColumn="Outlet_ID",
            parentTable="Outlets",
            parentColumn="Outlet_ID",
            sourceBusinessKey="Unknown Outlet",
            existingParentFound=False,
            status="NOT_FOUND"
        )
    ]

    summary = MigrationValidator.validate_dataset(
        df=df,
        schema=mock_schema,
        column_mappings=mappings,
        foreign_key_plans=fk_plan,
        parent_records_by_table={"Outlets": [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]}
    )

    assert summary.can_execute is False
    assert any(e.error_type == "UNRESOLVED_FOREIGN_KEY" for e in summary.errors)


# -------------------------------------------------------------------------------------------------
# TEST 7: Validation Passes When All Foreign Keys Resolved
# -------------------------------------------------------------------------------------------------
def test_7_validation_passes_when_all_fks_resolved(mock_schema):
    """
    When all required foreign keys are successfully resolved,
    validation should pass with can_execute = True.
    """
    df = pd.DataFrame({
        "Outlet_Name": ["ANN", "ANN"],
        "Total": [150.0, 200.0]
    })

    mappings = [
        ColumnMappingResult(excelColumn="Outlet_Name", databaseTable="Order", databaseColumn="Outlet_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="Total", databaseTable="Order", databaseColumn="Total", status="MATCHED")
    ]

    fk_plan = [
        ForeignKeyPlanItem(
            childTable="Order",
            childColumn="Outlet_ID",
            parentTable="Outlets",
            parentColumn="Outlet_ID",
            sourceBusinessKey="ANN",
            existingParentFound=True,
            resolvedId="69",
            status="MATCHED",
            valueSource="DATABASE_LOOKUP"
        )
    ]

    summary = MigrationValidator.validate_dataset(
        df=df,
        schema=mock_schema,
        column_mappings=mappings,
        foreign_key_plans=fk_plan,
        parent_records_by_table={"Outlets": [{"Outlet_ID": 69, "Outlet_Name": "ANN"}]}
    )

    # Required column Outlet_ID is mapped and FK plan is resolved
    assert summary.can_execute is True
    assert summary.unresolved_foreign_keys_count == 0


# -------------------------------------------------------------------------------------------------
# TEST 8: Full Preview with DATABASE_LOOKUP Value Source
# -------------------------------------------------------------------------------------------------
def test_8_preview_generation_with_database_lookup(mock_schema):
    """
    Tests end-to-end preview generation where Outlets in the database has Outlet_ID = 69,
    and Excel has Outlet_Name = 'ANN'.
    Verifies that preview row has final_value = '69' and value_source = 'DATABASE_LOOKUP'.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY, Outlet_Name TEXT)"))
        conn.execute(text("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY, Customer_Name TEXT)"))
        conn.execute(text("CREATE TABLE [Order] (Order_ID INTEGER PRIMARY KEY AUTOINCREMENT, Outlet_ID INTEGER NOT NULL, Customer_ID INTEGER, Total REAL)"))
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name) VALUES (69, 'ANN')"))

    df = pd.DataFrame({
        "Outlet_Name": ["ANN"],
        "Total": [250.0]
    })

    mappings = [
        ColumnMappingResult(excelColumn="Outlet_Name", databaseTable="Order", databaseColumn="Outlet_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="Total", databaseTable="Order", databaseColumn="Total", status="MATCHED")
    ]

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="Orders.xlsx",
        schema=mock_schema,
        column_mappings=mappings,
        engine=engine
    )

    rows = preview_res.preview_rows
    outlet_row = next((r for r in rows if r.target_table == "Order" and r.target_column == "Outlet_ID"), None)
    assert outlet_row is not None
    assert outlet_row.final_value == "69"
    assert outlet_row.value_source == "DATABASE_LOOKUP"
    assert outlet_row.validation_status == "VALID"


# -------------------------------------------------------------------------------------------------
# TEST 9: Reusable Dynamic Logic Across Multiple Entities (Customer, Category, Branch)
# -------------------------------------------------------------------------------------------------
def test_9_reusable_across_multiple_entities():
    """
    Verifies the same reusable dynamic logic applies to Customer, Category, or Branch.
    """
    customer_records = [
        {"Customer_ID": 501, "Customer_Name": "Rahul Sharma"},
        {"Customer_ID": 502, "Customer_Name": "Anita Desai"}
    ]

    res_id, matched_name, status, val_source, conf, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Rahul Sharma",
        parent_table="Customer",
        parent_id_column="Customer_ID",
        parent_name_column="Customer_Name",
        db_records=customer_records
    )

    assert status == "MATCHED"
    assert res_id == 501
    assert matched_name == "Rahul Sharma"
    assert val_source == "DATABASE_LOOKUP"

    # Branch / Category entity
    category_records = [
        {"Category_ID": 10, "Category_Name": "Beverages"},
        {"Category_ID": 20, "Category_Name": "Desserts"}
    ]

    cat_id, cat_name, cat_status, cat_source, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="beverages",
        parent_table="Categories",
        parent_id_column="Category_ID",
        parent_name_column="Category_Name",
        db_records=category_records
    )

    assert cat_status == "MATCHED"
    assert cat_id == 10
    assert cat_name == "Beverages"
    assert cat_source == "DATABASE_LOOKUP"

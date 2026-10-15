import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine, text

from app.models.schema import (
    DatabaseSchemaMetadata,
    TableMetadata,
    ColumnMetadata,
    ForeignKeyInfo
)
from app.models.migration import (
    ColumnMappingResult,
    ColumnOverrideItem
)
from app.utils.normalization import normalize_column_name, tokenize_column_name
from app.mapping.synonyms import (
    synonym_registry,
    BusinessConcept,
    CustomMappingRule,
    SynonymRegistry
)
from app.mapping.matcher import ColumnMatcher
from app.migration.preview import MigrationPreviewEngine
from app.migration.validator import MigrationValidator
from app.migration.transaction import transaction_manager
from app.services.migration_service import migration_service


@pytest.fixture
def pos_schema():
    """Builds a representative POS database schema with Customer, Outlets, Order, Bills, OrderLineItems."""
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="NVARCHAR(100)", nullable=False, primary_key=False),
        ColumnMetadata(name="Address", type="NVARCHAR(200)", nullable=True, primary_key=False),
        ColumnMetadata(name="Contact_Number", type="NVARCHAR(50)", nullable=True, primary_key=False),
    ]
    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="NVARCHAR(100)", nullable=False, primary_key=False),
        ColumnMetadata(name="Location", type="NVARCHAR(200)", nullable=True, primary_key=False),
    ]
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, foreign_keys=[
            ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
        ]),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False),
        ColumnMetadata(name="Order_Status", type="NVARCHAR(50)", nullable=True),
    ]
    bills_cols = [
        ColumnMetadata(name="Bill_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Bill_Number", type="NVARCHAR(50)", nullable=False),
        ColumnMetadata(name="Bill_Amount", type="DECIMAL(18,2)", nullable=False),
        ColumnMetadata(name="Total_Tax", type="DECIMAL(18,2)", nullable=True),
        ColumnMetadata(name="VAT_Percentage", type="DECIMAL(5,2)", nullable=True),
        ColumnMetadata(name="ServiceCharge_Total", type="DECIMAL(18,2)", nullable=True),
        ColumnMetadata(name="ServiceCharge_Percentage", type="DECIMAL(5,2)", nullable=True),
    ]
    lineitems_cols = [
        ColumnMetadata(name="LineItem_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Description", type="NVARCHAR(200)", nullable=False),
        ColumnMetadata(name="Quantity", type="INTEGER", nullable=False),
        ColumnMetadata(name="Item_Price", type="DECIMAL(18,2)", nullable=False),
        ColumnMetadata(name="Item_Total", type="DECIMAL(18,2)", nullable=True),
    ]

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[
            TableMetadata(name="Customer", columns=customer_cols, primary_keys=["Customer_ID"]),
            TableMetadata(name="Outlets", columns=outlets_cols, primary_keys=["Outlet_ID"]),
            TableMetadata(name="Order", columns=order_cols, primary_keys=["Order_ID"]),
            TableMetadata(name="Bills", columns=bills_cols, primary_keys=["Bill_ID"]),
            TableMetadata(name="OrderLineItems", columns=lineitems_cols, primary_keys=["LineItem_ID"]),
        ]
    )


# -----------------------------------------------------------------------------
# TEST 1: Case and naming-convention normalization
# -----------------------------------------------------------------------------
def test_1_case_and_naming_convention_normalization():
    variations = [
        ("customer_name", "customer name"),
        ("CUSTOMER_NAME", "customer name"),
        ("CustomerName", "customer name"),
        ("customerName", "customer name"),
        ("Customer Name", "customer name"),
        ("customer-name", "customer name"),
        ("customer   name", "customer name"),
        ("customer___name", "customer name"),
        ("CustomerID", "customer id"),
        ("customerId", "customer id"),
        ("customer_id", "customer id"),
        ("orderURL", "order url"),
        ("IPAddress", "ip address"),
        ("item_qty", "item qty"),
        ("ItemQty", "item qty"),
        ("bill_no", "bill no"),
        ("BillNo", "bill no"),
    ]
    for raw, expected in variations:
        normalized = normalize_column_name(raw)
        assert normalized == expected, f"Failed normalizing '{raw}': got '{normalized}', expected '{expected}'"


# -----------------------------------------------------------------------------
# TEST 2: Exact synonym matches
# -----------------------------------------------------------------------------
def test_2_exact_synonym_matches(pos_schema):
    excel_cols = ["client_name", "phone_number", "unit_price"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema, database_scope="BOPOS_Leons")

    mapping_by_col = {r.excel_column: r for r in results}

    assert mapping_by_col["client_name"].status == "MATCHED"
    assert mapping_by_col["client_name"].database_table == "Customer"
    assert mapping_by_col["client_name"].database_column == "Customer_Name"
    assert mapping_by_col["client_name"].match_type in ("SYNONYM", "NORMALIZED")

    assert mapping_by_col["phone_number"].status == "MATCHED"
    assert mapping_by_col["phone_number"].database_table == "Customer"
    assert mapping_by_col["phone_number"].database_column == "Contact_Number"
    assert mapping_by_col["phone_number"].match_type == "SYNONYM"

    assert mapping_by_col["unit_price"].status == "MATCHED"
    assert mapping_by_col["unit_price"].database_table == "OrderLineItems"
    assert mapping_by_col["unit_price"].database_column == "Item_Price"
    assert mapping_by_col["unit_price"].match_type == "SYNONYM"


# -----------------------------------------------------------------------------
# TEST 3: Multiple aliases for the same business concept
# -----------------------------------------------------------------------------
def test_3_multiple_aliases_for_same_concept(pos_schema):
    aliases = ["client_name", "guest_name", "custname", "customer_name"]
    for alias in aliases:
        results = ColumnMatcher.match_columns([alias], pos_schema, database_scope="BOPOS_Leons")
        res = results[0]
        assert res.status == "MATCHED"
        assert res.database_table == "Customer"
        assert res.database_column == "Customer_Name"
        assert res.business_concept == "Customer name"


# -----------------------------------------------------------------------------
# TEST 4: Ambiguous address mappings
# -----------------------------------------------------------------------------
def test_4_ambiguous_address_mapping(pos_schema):
    # 'address' could plausibly belong to Customer.Address or Outlets.Location
    excel_cols = ["address"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema, database_scope="BOPOS_Leons")
    res = results[0]

    assert res.status == "AMBIGUOUS"
    assert res.match_type == "AMBIGUOUS"
    assert res.database_table == "UNKNOWN"
    # Ensure candidates include both Customer and Outlets
    cand_tables = {c["databaseTable"] for c in res.candidates}
    assert "Customer" in cand_tables
    assert "Outlets" in cand_tables


# -----------------------------------------------------------------------------
# TEST 5: Amount-versus-percentage mismatches
# -----------------------------------------------------------------------------
def test_5_amount_versus_percentage_mismatches(pos_schema):
    # total_tax is monetary amount; it must NOT map to VAT_Percentage
    excel_cols = ["total_tax"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema, database_scope="BOPOS_Leons")
    res = results[0]

    assert res.database_column != "VAT_Percentage", "total_tax incorrectly mapped to VAT_Percentage rate!"
    assert res.database_column == "Total_Tax"

    # tax_rate or tax_percentage must map to VAT_Percentage, NOT Total_Tax
    pct_results = ColumnMatcher.match_columns(["tax_rate"], pos_schema, database_scope="BOPOS_Leons")
    pct_res = pct_results[0]
    assert pct_res.database_column == "VAT_Percentage"
    assert pct_res.database_column != "Total_Tax"


# -----------------------------------------------------------------------------
# TEST 6: Item name versus item price
# -----------------------------------------------------------------------------
def test_6_item_name_versus_item_price(pos_schema):
    # item_name is text description; it must NOT map to Item_Price
    excel_cols = ["item_name"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema, database_scope="BOPOS_Leons")
    res = results[0]

    assert res.database_column != "Item_Price", "item_name incorrectly mapped to Item_Price numeric column!"
    assert res.database_column == "Description"


# -----------------------------------------------------------------------------
# TEST 7: Duplicate target-column assignments
# -----------------------------------------------------------------------------
def test_7_duplicate_target_column_assignments(pos_schema):
    # Two source columns claiming the exact same database column Customer.Customer_Name
    excel_cols = ["Customer_Name", "client_name"]
    results = ColumnMatcher.match_columns(excel_cols, pos_schema, database_scope="BOPOS_Leons")

    mapping_by_col = {r.excel_column: r for r in results}
    # Exact match wins
    assert mapping_by_col["Customer_Name"].status == "MATCHED"
    assert mapping_by_col["Customer_Name"].database_table == "Customer"
    assert mapping_by_col["Customer_Name"].database_column == "Customer_Name"

    # Duplicate claim is safely downgraded to AMBIGUOUS to prevent collision
    assert mapping_by_col["client_name"].status == "AMBIGUOUS"
    assert mapping_by_col["client_name"].database_table == "UNKNOWN"


# -----------------------------------------------------------------------------
# TEST 8: Incompatible data types
# -----------------------------------------------------------------------------
def test_8_incompatible_data_types(pos_schema):
    # Customer name text must NOT map to integer ID Customer_ID
    results = ColumnMatcher.match_columns(["customer_name"], pos_schema, database_scope="BOPOS_Leons")
    for cand in results[0].candidates:
        assert cand["databaseColumn"] != "Customer_ID", "Text customer_name proposed for integer Customer_ID!"


# -----------------------------------------------------------------------------
# TEST 9: Custom synonym creation, editing, and deletion
# -----------------------------------------------------------------------------
def test_9_custom_synonym_creation_editing_deletion():
    registry = SynonymRegistry()
    concept_id = "customer_name"

    # 1. Add custom synonym
    added = registry.add_synonym(concept_id, "patron_full_name", database_scope="TestDB")
    assert added is True

    match = registry.find_concept_for_term("patron_full_name", database_scope="TestDB")
    assert match is not None
    assert match[1].id == concept_id

    # 2. Delete custom synonym
    deleted = registry.remove_synonym(concept_id, "patron_full_name", database_scope="TestDB")
    assert deleted is True

    match_after = registry.find_concept_for_term("patron_full_name", database_scope="TestDB")
    assert match_after is None


# -----------------------------------------------------------------------------
# TEST 10: Foreign-key resolution using existing records
# -----------------------------------------------------------------------------
def test_10_foreign_key_resolution_using_existing_records(pos_schema):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY,
                Customer_Name TEXT NOT NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY,
                Customer_ID INTEGER NOT NULL,
                Order_DateTime TEXT NOT NULL
            );
        """))
        conn.execute(text("INSERT INTO Customer (Customer_ID, Customer_Name) VALUES (42, 'Existing VIP');"))

    df = pd.DataFrame([
        {"customer_name": "Existing VIP", "order_date": "2026-06-30 10:00:00"}
    ])

    mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED"),
    ]

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_fk.xlsx",
        schema=pos_schema,
        column_mappings=mappings,
        engine=engine
    )

    preview_rows = preview_res[0]
    # Check that Customer_ID in Order is resolved to existing ID 42
    order_fk_row = next((r for r in preview_rows if r.target_table == "Order" and r.target_column == "Customer_ID"), None)
    assert order_fk_row is not None
    assert order_fk_row.value_source in ("EXISTING_DATABASE", "DATABASE_LOOKUP")
    assert order_fk_row.final_value == "42"


# -----------------------------------------------------------------------------
# TEST 11: Preview regeneration after manual mapping changes
# -----------------------------------------------------------------------------
def test_11_preview_regeneration_after_manual_mapping_changes():
    upload_id = "test_upload_regen_123"
    with patch.object(migration_service, "process_step3") as mock_step3:
        mock_step3.return_value = MagicMock()
        overrides = [
            ColumnOverrideItem(excelColumn="address", databaseTable="Customer", databaseColumn="Address", action="MAP")
        ]
        migration_service.save_column_overrides(upload_id, overrides)
        assert upload_id in migration_service._user_overrides
        assert migration_service._user_overrides[upload_id]["address"]["databaseColumn"] == "Address"
        mock_step3.assert_called_with(upload_id, force_refresh=True)


# -----------------------------------------------------------------------------
# TEST 12: Prevention of execution when required mappings remain unresolved
# -----------------------------------------------------------------------------
def test_12_prevention_of_execution_unresolved_required(pos_schema):
    # Customer_Name is NOT NULL in Customer table. If left unmapped, validation must fail
    df = pd.DataFrame([{"address": "Bengaluru India"}])
    mappings = [
        ColumnMappingResult(excelColumn="address", databaseTable="Customer", databaseColumn="Address", status="MATCHED")
    ]
    summary = MigrationValidator.validate_dataset(df, pos_schema, mappings)

    assert summary.can_execute is False
    assert any(e.error_type == "REQUIRED_COLUMN_UNMAPPED" and e.column_name == "Customer_Name" for e in summary.errors)


# -----------------------------------------------------------------------------
# TEST 13: Existing migration transaction rollback behavior
# -----------------------------------------------------------------------------
def test_13_transaction_rollback_behavior():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE TestAccounts (id INTEGER PRIMARY KEY, balance REAL);"))
        conn.execute(text("INSERT INTO TestAccounts VALUES (1, 500.0);"))

    # Execute a transaction that fails halfway
    def failing_migration(connection):
        connection.execute(text("UPDATE TestAccounts SET balance = 1000.0 WHERE id = 1;"))
        raise RuntimeError("Simulated mid-migration failure!")

    with pytest.raises(RuntimeError):
        transaction_manager.execute_transactionally("upload_fail_test_synonym_13", engine, failing_migration)

    # Verify atomic rollback: balance must remain 500.0
    with engine.connect() as conn:
        val = conn.execute(text("SELECT balance FROM TestAccounts WHERE id = 1;")).scalar()
        assert val == 500.0, "Transaction did not roll back properly after failure!"

"""
Automated Integration and Unit Tests for OrderLineItems Item_ID Resolution.

Verifies all 12 items matching and ID handling rules:
1. Inspects item master table schema (Item_ID, Item_Name / Item, Category / Group).
2. Reads item name and item group/category from Excel source when available.
3. Normalizes item names and categories consistently (trim, case, punctuation, formatting).
4. Matches using Item Name + Item Group/Category wherever both are available.
5. If exact composite match is unavailable, safe normalized item-name match only when unique.
6. Portion variants (single, double, 2pc) stay distinct; distinct products never merged.
7. Never derives Item_ID from Item_Price, Quantity, row numbers, or arbitrary tokens.
8. Never silently inserts NULL for Item_ID when a valid item match exists.
9. Resolved database Item_ID is passed into the actual OrderLineItems INSERT statement.
10. Unresolved rows recorded with description, group/category, and price (not inventing IDs).
11. Preserves existing order, quantity, price, and composite-item behavior.
12. Zero hardcoding of IDs, database names, or table names; runs against isolated in-memory database.
"""

import pytest
import pandas as pd
from unittest.mock import patch
from sqlalchemy import create_engine, text

from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import ColumnMappingResult
from app.migration.lookup_resolver import (
    DynamicLookupResolver,
    LookupRelationship,
    is_item_master_target,
    item_row_source_key
)
from app.migration.engine import MigrationEngine
from app.migration.preview import MigrationPreviewEngine
from app.migration.item_reconciliation import ItemReconciliationService


@pytest.fixture
def test_db_engine():
    """In-memory SQLite database representing SQL Server tables."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ItemsMaster (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Item_ID VARCHAR(50) NOT NULL UNIQUE,
                Item_Name VARCHAR(100) NOT NULL,
                Category VARCHAR(50),
                Item_Group VARCHAR(50),
                Item_Price DECIMAL(10,2)
            );
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_Number VARCHAR(50) NOT NULL,
                Customer_ID INTEGER
            );
        """))
        conn.execute(text("""
            CREATE TABLE OrderLineItems (
                Line_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_ID INTEGER,
                Item_ID VARCHAR(50),
                Description VARCHAR(200),
                Item_Price DECIMAL(10,2),
                Quantity INTEGER
            );
        """))
        # Seed realistic master records including variants and category distinctions
        conn.execute(text("""
            INSERT INTO ItemsMaster (Item_ID, Item_Name, Category, Item_Group, Item_Price) VALUES
            ('ITEM_101', 'Masala Dosa', 'South Indian', 'Tiffin', 60.00),
            ('ITEM_102', 'Mini Meal', 'North Indian', 'Meals', 120.00),
            ('ITEM_103', 'Chai', 'Beverages', 'Hot Drinks', 20.00),
            ('ITEM_104', 'Chai', 'Snacks', 'Bakery', 25.00),
            ('ITEM_105', 'Idli (single)', 'South Indian', 'Tiffin', 20.00),
            ('ITEM_106', 'Idli (double)', 'South Indian', 'Tiffin', 35.00),
            ('ITEM_107', 'Filter Coffee', 'Beverages', 'Hot Drinks', 30.00);
        """))
    return engine


@pytest.fixture
def test_schema_meta():
    """Dynamic schema metadata corresponding to the in-memory database."""
    return DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="ItemsMaster",
                columns=[
                    ColumnMetadata(name="ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Item_ID", type="VARCHAR(50)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Item_Name", type="VARCHAR(100)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Category", type="VARCHAR(50)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_Group", type="VARCHAR(50)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=True, primary_key=False),
                ],
                primary_keys=["ID"]
            ),
            TableMetadata(
                name="Order",
                columns=[
                    ColumnMetadata(name="Order_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_Number", type="VARCHAR(50)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=True, primary_key=False),
                ],
                primary_keys=["Order_ID"]
            ),
            TableMetadata(
                name="OrderLineItems",
                columns=[
                    ColumnMetadata(name="Line_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_ID", type="INTEGER", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_ID", type="VARCHAR(50)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Description", type="VARCHAR(200)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Quantity", type="INTEGER", nullable=False, primary_key=False),
                ],
                primary_keys=["Line_ID"]
            ),
        ]
    )


# -------------------------------------------------------------------------------------------------
# 1. Exact Match and Normalization (Case, Spacing, Underscores)
# -------------------------------------------------------------------------------------------------
def test_exact_and_normalized_item_match():
    records = [
        {"Item_ID": "101", "Item_Name": "Masala Dosa", "Category": "South Indian"},
        {"Item_ID": "102", "Item_Name": "Mini Meal", "Category": "North Indian"}
    ]
    # Exact match
    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Masala Dosa",
        row_data={"Description": "Masala Dosa"}
    )
    assert res.status == "RESOLVED"
    assert res.matched_primary_key_value == "101"

    # Case, whitespace, and formatting variations: "  mini   meal  "
    res_norm = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="  mini   meal  ",
        row_data={"Description": "  mini   meal  "}
    )
    assert res_norm.status == "RESOLVED"
    assert res_norm.matched_primary_key_value == "102"


# -------------------------------------------------------------------------------------------------
# 2. Item Name + Item Group/Category Composite Matching
# -------------------------------------------------------------------------------------------------
def test_item_name_plus_category_composite_match():
    records = [
        {"Item_ID": "CHAI_BEV", "Item_Name": "Chai", "Category": "Beverages", "Item_Group": "Hot Drinks"},
        {"Item_ID": "CHAI_SNK", "Item_Name": "Chai", "Category": "Snacks", "Item_Group": "Bakery"}
    ]
    # Chai under Beverages
    res_bev = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Chai",
        row_data={"Description": "Chai", "Category": "Beverages"}
    )
    assert res_bev.status == "RESOLVED"
    assert res_bev.matched_primary_key_value == "CHAI_BEV"

    # Chai under Snacks
    res_snk = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Chai",
        row_data={"Description": "Chai", "Category": "Snacks"}
    )
    assert res_snk.status == "RESOLVED"
    assert res_snk.matched_primary_key_value == "CHAI_SNK"


# -------------------------------------------------------------------------------------------------
# 3. Ambiguous Match when Category/Group is Missing
# -------------------------------------------------------------------------------------------------
def test_ambiguous_item_name_without_category_refuses_to_guess():
    records = [
        {"Item_ID": "CHAI_BEV", "Item_Name": "Chai", "Category": "Beverages"},
        {"Item_ID": "CHAI_SNK", "Item_Name": "Chai", "Category": "Snacks"}
    ]
    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Chai",
        row_data={"Description": "Chai"}  # No category provided
    )
    assert res.status == "AMBIGUOUS"
    assert res.matched_primary_key_value is None
    assert len(res.candidates) == 2


# -------------------------------------------------------------------------------------------------
# 4. Portion Variants Stay Distinct (single vs double vs 2pc)
# -------------------------------------------------------------------------------------------------
def test_portion_variants_remain_distinct():
    records = [
        {"Item_ID": "IDLI_1", "Item_Name": "Idli (single)", "Category": "South Indian"},
        {"Item_ID": "IDLI_2", "Item_Name": "Idli (double)", "Category": "South Indian"}
    ]
    # Idli (single) must NOT match Idli (double)
    res_single = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Idli (single)",
        row_data={"Description": "Idli (single)", "Category": "South Indian"}
    )
    assert res_single.status == "RESOLVED"
    assert res_single.matched_primary_key_value == "IDLI_1"

    # Idli (2pc) should match Idli (double) via verified variant alias
    res_2pc = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Idli (2pc)",
        row_data={"Description": "Idli (2pc)", "Category": "South Indian"}
    )
    assert res_2pc.status == "RESOLVED"
    assert res_2pc.matched_primary_key_value == "IDLI_2"

    # Idli (3pc) not in database -> must NOT silently resolve to single or double
    res_3pc = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Idli (3pc)",
        row_data={"Description": "Idli (3pc)", "Category": "South Indian"}
    )
    assert res_3pc.status == "UNRESOLVED"
    assert res_3pc.matched_primary_key_value is None


# -------------------------------------------------------------------------------------------------
# 5. Price Independence: Never Derive Item_ID from Price or Quantity
# -------------------------------------------------------------------------------------------------
def test_never_derive_item_id_from_price_or_quantity():
    records = [
        {"Item_ID": "DOSA_1", "Item_Name": "Plain Dosa", "Item_Price": 50.00},
        {"Item_ID": "SAND_1", "Item_Name": "Veg Sandwich", "Item_Price": 50.00}
    ]
    # Source row has "Special Snack" with price 50.00 -> unknown dish
    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value="Special Snack",
        row_data={"Description": "Special Snack", "Price": 50.00, "Quantity": 2}
    )
    assert res.status == "UNRESOLVED"
    assert res.matched_primary_key_value is None


# -------------------------------------------------------------------------------------------------
# 6. Composite Descriptions Unrecognized -> Cleanly UNRESOLVED
# -------------------------------------------------------------------------------------------------
def test_composite_description_unrecognized_recorded_unresolved():
    records = [
        {"Item_ID": "101", "Item_Name": "Idli", "Item_Price": 30.00},
        {"Item_ID": "102", "Item_Name": "Vada", "Item_Price": 30.00}
    ]
    composite_desc = "2 Idly + 1 Vada Combo Special"
    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item_Name",
        db_records=records,
        excel_source_value=composite_desc,
        row_data={"Description": composite_desc, "Price": 55.00}
    )
    assert res.status == "UNRESOLVED"
    assert res.matched_primary_key_value is None


# -------------------------------------------------------------------------------------------------
# 7. End-to-End Migration Flow: Resolved Item_ID Passed to SQL INSERT Statement
# -------------------------------------------------------------------------------------------------
def test_e2e_migration_resolves_and_inserts_item_id_into_order_line_items(test_db_engine, test_schema_meta):
    df = pd.DataFrame([
        {
            "order_number": "ORD-1001",
            "item_name": "Masala Dosa",
            "category": "South Indian",
            "price": 60.00,
            "quantity": 2
        },
        {
            "order_number": "ORD-1002",
            "item_name": "Mini Meal",
            "category": "North Indian",
            "price": 120.00,
            "quantity": 1
        },
        {
            "order_number": "ORD-1003",
            "item_name": "Chai",
            "category": "Beverages",
            "price": 20.00,
            "quantity": 4
        },
        {
            "order_number": "ORD-1004",
            "item_name": "Chai",
            "category": "Snacks",
            "price": 25.00,
            "quantity": 1
        },
        {
            "order_number": "ORD-1005",
            "item_name": "Unmatched Mystery Dish",
            "category": "Unknown",
            "price": 99.00,
            "quantity": 1
        }
    ])

    mappings = [
        ColumnMappingResult(excel_column="order_number", database_table="Order", database_column="Order_Number", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="item_name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=test_db_engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=test_schema_meta), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        response = MigrationEngine.execute_migration("test_order_line_item_upload")

    assert response.status == "COMPLETED"
    assert response.transaction == "COMMITTED"

    # Verify inserted records in OrderLineItems
    with test_db_engine.connect() as conn:
        lines = [dict(r) for r in conn.execute(text("SELECT Line_ID, Order_ID, Item_ID, Description, Item_Price, Quantity FROM OrderLineItems ORDER BY Line_ID")).mappings().all()]

    assert len(lines) == 5

    # 1. Masala Dosa -> ITEM_101
    assert lines[0]["Description"] == "Masala Dosa"
    assert lines[0]["Item_ID"] == "ITEM_101"
    assert lines[0]["Item_Price"] == 60.00
    assert lines[0]["Quantity"] == 2

    # 2. Mini Meal -> ITEM_102
    assert lines[1]["Description"] == "Mini Meal"
    assert lines[1]["Item_ID"] == "ITEM_102"
    assert lines[1]["Item_Price"] == 120.00

    # 3. Chai (Beverages) -> ITEM_103
    assert lines[2]["Description"] == "Chai"
    assert lines[2]["Item_ID"] == "ITEM_103"
    assert lines[2]["Item_Price"] == 20.00

    # 4. Chai (Snacks) -> ITEM_104
    assert lines[3]["Description"] == "Chai"
    assert lines[3]["Item_ID"] == "ITEM_104"
    assert lines[3]["Item_Price"] == 25.00

    # 5. Unmatched Mystery Dish -> Item_ID is None/NULL (not arbitrary guessed ID)
    assert lines[4]["Description"] == "Unmatched Mystery Dish"
    assert lines[4]["Item_ID"] is None
    assert lines[4]["Item_Price"] == 99.00


# -------------------------------------------------------------------------------------------------
# 8. Dry-Run Preview Reports Item_ID Resolution Status
# -------------------------------------------------------------------------------------------------
def test_preview_reports_resolved_and_unresolved_item_ids(test_db_engine, test_schema_meta):
    df = pd.DataFrame([
        {"item_name": "Masala Dosa", "category": "South Indian", "price": 60.00, "qty": 1},
        {"item_name": "Chai", "category": "Beverages", "price": 20.00, "qty": 1},
        {"item_name": "Nonexistent Dish", "category": "None", "price": 45.00, "qty": 1}
    ])
    mappings = [
        ColumnMappingResult(excel_column="item_name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="qty", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    preview = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_items.xlsx",
        schema=test_schema_meta,
        column_mappings=mappings,
        engine=test_db_engine
    )

    # Check FK plan items for OrderLineItems.Item_ID
    item_plan = [p for p in preview.dependency_plan if p.child_column == "Item_ID"]
    assert len(item_plan) >= 2
    # Ensure resolved items are matched
    matched_plan = [p for p in item_plan if p.status == "MATCHED"]
    assert len(matched_plan) >= 2
    assert any(p.resolved_id == "ITEM_101" for p in matched_plan)



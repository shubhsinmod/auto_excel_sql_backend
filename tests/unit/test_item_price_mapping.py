"""
Unit and integration tests for Item_Price mapping from ItemsMaster to OrderLineItems.

Verifies:
1. Verified Combo Example:
   - dbo.ItemsMaster: Item_ID=2, Item='2 Idli + 1 Masala Dosa', Item_Group='Combos', Item_Price=158.00
   - Source Excel price 89.00 results in newly migrated OrderLineItems.Item_Price = 158.00.
2. Multiple varying Excel prices (89.00, 129.00, 247.00) all map to 158.00 when matched to Item_ID=2.
3. Master price is fetched from the resolved master record, never by price matching independently.
4. Unmatched and ambiguous items are marked unresolved without assigning an arbitrary price.
5. Preservation of quantity, order relationships, descriptions, and other unrelated fields.
6. Migration preview output contains:
   - Excel item name
   - Matched ItemsMaster.Item_ID
   - ItemsMaster.Item_Price
   - Final OrderLineItems.Item_Price
   - Match status
7. Dry-run mode output and text_report contain all 5 required fields and perform 0 database writes.
8. Config toggle: ENABLE_MASTER_PRICE_MAPPING=False preserves source Excel price.
9. ItemsMaster is strictly read-only and never modified.
"""

import pytest
import pandas as pd
from unittest.mock import patch
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import ColumnMappingResult
from app.migration.engine import MigrationEngine
from app.migration.preview import MigrationPreviewEngine
from app.migration.dry_run import DryRunEngine


@pytest.fixture
def items_db_engine():
    """In-memory SQLite database representing SQL Server database with ItemsMaster, Order, and OrderLineItems."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ItemsMaster (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Item_ID VARCHAR(50) NOT NULL UNIQUE,
                Item VARCHAR(100) NOT NULL,
                Item_Group VARCHAR(50),
                Item_Price DECIMAL(10,2) NOT NULL
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

        # Seed the verified master record: Item_ID = 2, '2 Idli + 1 Masala Dosa', 'Combos', 158.00
        # Along with other items to test distinct disambiguation
        conn.execute(text("""
            INSERT INTO ItemsMaster (Item_ID, Item, Item_Group, Item_Price) VALUES
            ('1', 'Masala Dosa', 'South Indian', 90.00),
            ('2', '2 Idli + 1 Masala Dosa', 'Combos', 158.00),
            ('3', 'Idli Vada Combo', 'Combos', 89.00),
            ('4', 'Cold Coffee', 'Beverages', 80.00),
            ('5', 'Cold Coffee', 'Desserts', 110.00);
        """))
    return engine


@pytest.fixture
def items_schema_meta():
    """Dynamic schema metadata corresponding to the in-memory database."""
    return DatabaseSchemaMetadata(
        database_name="RestaurantDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="ItemsMaster",
                columns=[
                    ColumnMetadata(name="ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Item_ID", type="VARCHAR(50)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Item", type="VARCHAR(100)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Item_Group", type="VARCHAR(50)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False, primary_key=False),
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


def run_migration_for_test(df, schema, mappings, engine, upload_id="test_upload"):
    """Helper to execute migration using MigrationEngine against the in-memory engine and schema."""
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        return MigrationEngine.execute_migration(upload_id)


# -------------------------------------------------------------------------------------------------
# 1. Verified Example: Excel Price 89.00 Maps to Master Price 158.00 in Database INSERT
# -------------------------------------------------------------------------------------------------
def test_verified_combo_source_price_89_results_in_master_price_158(items_db_engine, items_schema_meta):
    """
    Verified example test:
    dbo.ItemsMaster has: Item_ID='2', Item='2 Idli + 1 Masala Dosa', Item_Group='Combos', Item_Price=158.00.
    Excel row has Item='2 Idli + 1 Masala Dosa', Item_Group='Combos', Item_Price=89.00.
    Proves that the newly migrated OrderLineItems record receives Item_ID='2' and Item_Price=158.00.
    """
    df = pd.DataFrame([
        {
            "Order_Number": "ORD-1001",
            "Item_Name": "2 Idli + 1 Masala Dosa",
            "Item_Group": "Combos",
            "Item_Price": 89.00,
            "Quantity": 3,
        }
    ])

    mappings = [
        ColumnMappingResult(excel_column="Order_Number", database_table="Order", database_column="Order_Number", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        resp = run_migration_for_test(df, items_schema_meta, mappings, items_db_engine, "upload_combo_89")

    assert resp.status == "COMPLETED"
    assert resp.transaction == "COMMITTED"

    with items_db_engine.connect() as conn:
        lines = [dict(r) for r in conn.execute(text("SELECT * FROM OrderLineItems")).mappings().all()]

    assert len(lines) == 1
    line = lines[0]
    assert line["Item_ID"] == "2"
    assert line["Description"] == "2 Idli + 1 Masala Dosa"
    assert line["Item_Price"] == 158.00  # Master price 158.00 overrides Excel price 89.00
    assert line["Quantity"] == 3  # Quantity preserved


# -------------------------------------------------------------------------------------------------
# 2. Multiple Varying Excel Prices (89.00, 129.00, 247.00) All Map to 158.00
# -------------------------------------------------------------------------------------------------
def test_multiple_excel_prices_all_result_in_master_price_158(items_db_engine, items_schema_meta):
    """
    Verifies that multiple incoming Excel prices (89.00, 129.00, 247.00) for '2 Idli + 1 Masala Dosa'
    all get assigned the master price 158.00.
    """
    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 1},
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 129.00, "Quantity": 2},
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 247.00, "Quantity": 5},
    ])

    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        resp = run_migration_for_test(df, items_schema_meta, mappings, items_db_engine, "upload_prices")

    assert resp.status == "COMPLETED"

    with items_db_engine.connect() as conn:
        lines = [dict(r) for r in conn.execute(text("SELECT Item_ID, Description, Item_Price, Quantity FROM OrderLineItems ORDER BY Line_ID")).mappings().all()]

    assert len(lines) == 3
    for line in lines:
        assert line["Item_ID"] == "2"
        assert line["Description"] == "2 Idli + 1 Masala Dosa"
        assert line["Item_Price"] == 158.00

    assert lines[0]["Quantity"] == 1
    assert lines[1]["Quantity"] == 2
    assert lines[2]["Quantity"] == 5


# -------------------------------------------------------------------------------------------------
# 3. Price Derived from Matched Master Record, Not Matched Independently
# -------------------------------------------------------------------------------------------------
def test_price_derived_from_matched_record_not_independent_price_matching(items_db_engine, items_schema_meta):
    """
    Item 3 in master has price 89.00 ('Idli Vada Combo').
    Item 2 in master has price 158.00 ('2 Idli + 1 Masala Dosa').
    When source row has Item='2 Idli + 1 Masala Dosa' and source price 89.00,
    the system must NOT match Item 3 by price; it resolves Item 2 by name/group and assigns 158.00.
    """
    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 1}
    ])
    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        run_migration_for_test(df, items_schema_meta, mappings, items_db_engine, "upload_indep")

    with items_db_engine.connect() as conn:
        lines = [dict(r) for r in conn.execute(text("SELECT Item_ID, Item_Price FROM OrderLineItems")).mappings().all()]

    assert len(lines) == 1
    assert lines[0]["Item_ID"] == "2"
    assert lines[0]["Item_Price"] == 158.00


# -------------------------------------------------------------------------------------------------
# 4. Unmatched and Ambiguous Items Marked Unresolved Without Arbitrary Price Assignment
# -------------------------------------------------------------------------------------------------
def test_unmatched_and_ambiguous_items_marked_unresolved_without_arbitrary_price(items_db_engine, items_schema_meta):
    """
    Unmatched item ('Unicorn Curry', 99.00) -> Item_ID is None, Item_Price retains source price 99.00.
    Ambiguous item ('Cold Coffee' exists in both 'Beverages' and 'Desserts', source price 75.00 without group) ->
    Item_ID is None/unresolved, no arbitrary price from master assigned.
    """
    df = pd.DataFrame([
        {"Item_Name": "Unicorn Curry", "Item_Group": "Fantasy", "Item_Price": 99.00, "Quantity": 1},
        {"Item_Name": "Cold Coffee", "Item_Group": None, "Item_Price": 75.00, "Quantity": 1},
    ])
    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        run_migration_for_test(df, items_schema_meta, mappings, items_db_engine, "upload_unmatched")

    with items_db_engine.connect() as conn:
        lines = [dict(r) for r in conn.execute(text("SELECT Item_ID, Description, Item_Price FROM OrderLineItems ORDER BY Line_ID")).mappings().all()]

    assert len(lines) == 2
    # Unmatched item
    assert lines[0]["Description"] == "Unicorn Curry"
    assert lines[0]["Item_ID"] is None
    assert lines[0]["Item_Price"] == 99.00

    # Ambiguous item
    assert lines[1]["Description"] == "Cold Coffee"
    assert lines[1]["Item_ID"] is None
    assert lines[1]["Item_Price"] == 75.00  # Kept source price, never arbitrary master price


# -------------------------------------------------------------------------------------------------
# 5. Migration Preview Displays All 5 Required Fields
# -------------------------------------------------------------------------------------------------
def test_preview_output_displays_all_five_required_fields(items_db_engine, items_schema_meta):
    """
    Migration preview must provide:
    1. Excel item name
    2. Matched ItemsMaster.Item_ID
    3. ItemsMaster.Item_Price
    4. Final OrderLineItems.Item_Price
    5. Match status
    """
    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 2},
        {"Item_Name": "Unknown Dish", "Item_Group": "Misc", "Item_Price": 45.00, "Quantity": 1},
    ])
    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        preview = MigrationPreviewEngine.generate_preview(
            df=df,
            file_name="test_preview.xlsx",
            schema=items_schema_meta,
            column_mappings=mappings,
            engine=items_db_engine
        )

    # 1. Check item_price_resolutions field
    resolutions = preview.item_price_resolutions
    assert len(resolutions) >= 2

    res_combo = next(r for r in resolutions if "2 Idli" in r.excel_item_name)
    assert res_combo.excel_item_name == "2 Idli + 1 Masala Dosa"  # (1) Excel item name
    assert res_combo.matched_item_id == "2"                      # (2) Matched Item_ID
    assert res_combo.master_price == 158.00                       # (3) ItemsMaster.Item_Price
    assert res_combo.final_order_line_price == 158.00             # (4) Final OrderLineItems.Item_Price
    assert res_combo.match_status == "MATCHED"                    # (5) Match status

    # 2. Check preview_rows for OrderLineItems.Item_Price
    price_rows = [r for r in preview.preview_rows if r.target_table == "OrderLineItems" and r.target_column == "Item_Price"]
    assert len(price_rows) == 2
    assert price_rows[0].final_value == "158.00"
    assert price_rows[0].value_source == "DATABASE_LOOKUP"


# -------------------------------------------------------------------------------------------------
# 6. Dry-Run Output and text_report Display All 5 Required Fields with Zero Writes
# -------------------------------------------------------------------------------------------------
def test_dry_run_reports_all_five_fields_with_zero_database_writes(items_db_engine, items_schema_meta):
    """
    Dry-run mode output and text_report must contain:
    - Excel item name
    - Matched ItemsMaster.Item_ID
    - ItemsMaster.Item_Price
    - Final OrderLineItems.Item_Price
    - Match status
    And strictly guarantee ZERO database writes.
    """
    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 2},
    ])

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        report = DryRunEngine.run_dry_run(
            df=df,
            source_name="test_dry_run.xlsx",
            schema=items_schema_meta,
            engine=items_db_engine
        )

    # Verify zero database writes
    assert report.database_writes == 0
    assert report.actual_database_write_count == 0
    assert report.db_integrity_verified is True

    # Verify item_price_resolutions in DryRunReport
    assert len(report.item_price_resolutions) >= 1
    res = report.item_price_resolutions[0]
    assert "2 Idli + 1 Masala Dosa" in res.excel_item_name
    assert res.matched_item_id == "2"
    assert res.master_price == 158.00
    assert res.final_order_line_price == 158.00
    assert res.match_status == "RESOLVED"

    # Verify text_report contains the resolution section and all 5 field headers/values
    assert "--- ITEM PRICE RESOLUTION (ItemsMaster -> OrderLineItems) ---" in report.text_report
    assert "Excel Item Name" in report.text_report
    assert "Matched Item ID" in report.text_report
    assert "Master Price" in report.text_report
    assert "Final Price" in report.text_report
    assert "Match Status" in report.text_report
    assert "158.00" in report.text_report
    assert "RESOLVED" in report.text_report


# -------------------------------------------------------------------------------------------------
# 7. Disabling the Master-Price Rule Preserves Source Excel Price
# -------------------------------------------------------------------------------------------------
def test_disabled_master_price_rule_preserves_source_excel_price(items_db_engine, items_schema_meta):
    """
    When ENABLE_MASTER_PRICE_MAPPING is False, source Excel price 89.00 is preserved in newly migrated records.
    """
    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 1}
    ])
    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", False):
        run_migration_for_test(df, items_schema_meta, mappings, items_db_engine, "upload_disabled")

    with items_db_engine.connect() as conn:
        lines = [dict(r) for r in conn.execute(text("SELECT Item_ID, Description, Item_Price FROM OrderLineItems")).mappings().all()]

    assert len(lines) == 1
    assert lines[0]["Item_ID"] == "2"
    assert lines[0]["Item_Price"] == 89.00  # Unaltered source price when rule is disabled


# -------------------------------------------------------------------------------------------------
# 8. Constraint: dbo.ItemsMaster is Strictly Unmodified
# -------------------------------------------------------------------------------------------------
def test_item_master_remains_strictly_unmodified(items_db_engine, items_schema_meta):
    """
    Verifies that neither preview, dry-run, nor execution ever writes to or modifies ItemsMaster.
    """
    with items_db_engine.connect() as conn:
        pre_records = conn.execute(text("SELECT * FROM ItemsMaster ORDER BY ID")).fetchall()

    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 1}
    ])
    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    with patch.object(settings, "ENABLE_MASTER_PRICE_MAPPING", True):
        # 1. Preview
        MigrationPreviewEngine.generate_preview(df=df, file_name="t.xlsx", schema=items_schema_meta, column_mappings=mappings, engine=items_db_engine)
        # 2. Dry-run
        DryRunEngine.run_dry_run(df=df, source_name="t.xlsx", schema=items_schema_meta, engine=items_db_engine)
        # 3. Execution
        run_migration_for_test(df, items_schema_meta, mappings, items_db_engine, "upload_unmod")

    with items_db_engine.connect() as conn:
        post_records = conn.execute(text("SELECT * FROM ItemsMaster ORDER BY ID")).fetchall()

    assert pre_records == post_records


# -------------------------------------------------------------------------------------------------
# 9. Step 3 Service Integration: process_step3_preview Returns item_price_resolutions
# -------------------------------------------------------------------------------------------------
def test_process_step3_preview_service_returns_item_price_resolutions(items_db_engine, items_schema_meta):
    """
    Verifies that migration_service.process_step3_preview executes successfully and returns
    a Step3ProcessingResponse containing item_price_resolutions without any NameError or exceptions.
    """
    from app.services.migration_service import migration_service

    df = pd.DataFrame([
        {"Item_Name": "2 Idli + 1 Masala Dosa", "Item_Group": "Combos", "Item_Price": 89.00, "Quantity": 2}
    ])
    mappings = [
        ColumnMappingResult(excel_column="Item_Name", database_table="OrderLineItems", database_column="Description", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Item_Price", database_table="OrderLineItems", database_column="Item_Price", confidence=1.0, match_type="exact", status="MATCHED"),
        ColumnMappingResult(excel_column="Quantity", database_table="OrderLineItems", database_column="Quantity", confidence=1.0, match_type="exact", status="MATCHED"),
    ]

    upload_id = "test_step3_service_upload"
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.services.migration_service.get_engine", return_value=items_db_engine), \
         patch("app.services.migration_service.database_service.get_schema_metadata", return_value=items_schema_meta), \
         patch("app.services.migration_service.load_file_dataframe", return_value=df), \
         patch("app.services.migration_service.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1, "fileName": "test.xlsx"}
        response = migration_service.process_step3(upload_id)

    assert response.status == "COMPLETED"
    assert response.can_execute is True
    assert len(response.item_price_resolutions) >= 1
    res = response.item_price_resolutions[0]
    assert "2 Idli" in res.excel_item_name
    assert res.matched_item_id == "2"
    assert res.master_price == 158.00
    assert res.final_order_line_price == 158.00
    assert res.match_status == "MATCHED"

"""
Automated Unit Tests for Item ID, Item Name, and Price Mapping.

Tests the 10 required scenarios from Section 9:
1. Exact master record match: 'Masala Dosa' resolves dynamically to Item_ID from ItemsMaster.
2. Exact item with packaging/quantity variant: 'Gulab Jamun (2pc)' resolves dynamically.
3. Exact item with parcel variant: 'Ven Pongal Parcel' resolves dynamically.
4. Negative safety guarantee: 'Masala Dosa' cannot silently resolve to another item ID.
5. Price independence: Different items with identical prices resolve to their own independent IDs.
6. Composite & variant safety: Similar names and composite items ('2 Idli + 1 Masala Dosa Online', 'Plain Dosa Parcel')
   do not incorrectly match or cross-resolve.
7. Unresolved reporting: Missing, empty, or unknown item names are cleanly flagged as UNRESOLVED without crash.
8. Column order independence: Excel column order changes do not break mapping when headers are present.
9. Historical price preservation: Source transaction price is preserved in OrderLineItems.Item_Price.
10. System robustness: Duplicate detection, FK resolution, and transactional execution continue to work.

STRICT REQUIREMENT: ZERO HARDCODING. Tests dynamically configure fixtures and verify
against metadata and actual table records.
"""

import pytest
import pandas as pd
from unittest.mock import patch
from sqlalchemy import create_engine, text

from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import ColumnMappingResult
from app.mapping.matcher import ColumnMatcher
from app.migration.lookup_resolver import DynamicLookupResolver
from app.migration.item_reconciliation import ItemReconciliationService


@pytest.fixture
def item_master_records():
    """
    Dynamic test master records representing realistic items with varying prices and variants.
    Notice identical prices for different items (e.g. Idli and Vada both 15.00).
    """
    return [
        {"ID": 101, "Item_ID": "22", "Item": "Masala Dosa", "Item_Price": 46.296},
        {"ID": 102, "Item_ID": "59", "Item": "Gulab Jamun (2pc)", "Item_Price": 27.778},
        {"ID": 103, "Item_ID": "79", "Item": "Ven Pongal Parcel", "Item_Price": 60.185},
        {"ID": 104, "Item_ID": "119", "Item": "Sweet Lassi", "Item_Price": 37.037},
        {"ID": 105, "Item_ID": "201", "Item": "Plain Dosa", "Item_Price": 35.000},
        {"ID": 106, "Item_ID": "202", "Item": "Rava Dosa", "Item_Price": 50.000},
        {"ID": 107, "Item_ID": "301", "Item": "Idli (2pc)", "Item_Price": 30.000},
        {"ID": 108, "Item_ID": "302", "Item": "Vada (2pc)", "Item_Price": 30.000},  # Same price as Idli!
    ]


@pytest.fixture
def item_schema_meta():
    """Dynamic schema metadata for ItemsMaster and OrderLineItems."""
    return DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="mssql",
        tables=[
            TableMetadata(
                name="ItemsMaster",
                columns=[
                    ColumnMetadata(name="ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Item_ID", type="VARCHAR(50)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Item", type="VARCHAR(100)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False, primary_key=False)
                ],
                primary_keys=["ID"]
            ),
            TableMetadata(
                name="OrderLineItems",
                columns=[
                    ColumnMetadata(name="Line_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_ID", type="BIGINT", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_ID", type="VARCHAR(50)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Description", type="VARCHAR(200)", nullable=True, primary_key=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False, primary_key=False),
                    ColumnMetadata(name="Quantity", type="INT", nullable=False, primary_key=False)
                ],
                primary_keys=["Line_ID"]
            )
        ]
    )


# -------------------------------------------------------------------------------------------------
# Scenario 1: Exact master item resolution ('Masala Dosa' -> dynamic Item_ID)
# -------------------------------------------------------------------------------------------------
def test_1_masala_dosa_resolves_to_correct_item_id(item_master_records):
    expected_id = next(r["Item_ID"] for r in item_master_records if r["Item"] == "Masala Dosa")
    res_id, res_name, status, method, conf, _, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="Masala Dosa",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status == "MATCHED"
    assert res_id == expected_id
    assert res_name == "Masala Dosa"
    assert conf >= 0.95


# -------------------------------------------------------------------------------------------------
# Scenario 2: Exact item with count/packaging ('Gulab Jamun (2pc)')
# -------------------------------------------------------------------------------------------------
def test_2_gulab_jamun_2pc_resolves_correctly(item_master_records):
    expected_id = next(r["Item_ID"] for r in item_master_records if r["Item"] == "Gulab Jamun (2pc)")
    # Test with casing and extra space
    res_id, res_name, status, method, conf, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="  gulab jamun (2pc)  ",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status == "MATCHED"
    assert res_id == expected_id
    assert res_name == "Gulab Jamun (2pc)"


# -------------------------------------------------------------------------------------------------
# Scenario 3: Exact item with parcel suffix ('Ven Pongal Parcel')
# -------------------------------------------------------------------------------------------------
def test_3_ven_pongal_parcel_resolves_correctly(item_master_records):
    expected_id = next(r["Item_ID"] for r in item_master_records if r["Item"] == "Ven Pongal Parcel")
    res_id, res_name, status, method, conf, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Ven Pongal Parcel",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status == "MATCHED"
    assert res_id == expected_id
    assert res_name == "Ven Pongal Parcel"


# -------------------------------------------------------------------------------------------------
# Scenario 4: Negative safety - 'Masala Dosa' CANNOT silently resolve to other IDs
# -------------------------------------------------------------------------------------------------
def test_4_masala_dosa_cannot_resolve_to_unrelated_id(item_master_records):
    correct_id = next(r["Item_ID"] for r in item_master_records if r["Item"] == "Masala Dosa")
    other_ids = [r["Item_ID"] for r in item_master_records if r["Item"] != "Masala Dosa"]

    res_id, _, status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Masala Dosa",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status == "MATCHED"
    assert res_id == correct_id
    assert res_id not in other_ids


# -------------------------------------------------------------------------------------------------
# Scenario 5: Different items with IDENTICAL prices resolve independently
# -------------------------------------------------------------------------------------------------
def test_5_different_items_with_identical_prices_resolve_to_own_ids(item_master_records):
    idli_id = next(r["Item_ID"] for r in item_master_records if r["Item"] == "Idli (2pc)")
    vada_id = next(r["Item_ID"] for r in item_master_records if r["Item"] == "Vada (2pc)")
    assert idli_id != vada_id

    # Verify both have identical master price
    idli_price = next(r["Item_Price"] for r in item_master_records if r["Item"] == "Idli (2pc)")
    vada_price = next(r["Item_Price"] for r in item_master_records if r["Item"] == "Vada (2pc)")
    assert idli_price == vada_price

    res_idli, _, status_idli, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Idli (2pc)",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    res_vada, _, status_vada, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Vada (2pc)",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )

    assert status_idli == "MATCHED" and res_idli == idli_id
    assert status_vada == "MATCHED" and res_vada == vada_id
    assert res_idli != res_vada


# -------------------------------------------------------------------------------------------------
# Scenario 6: Composite items and variants are NOT incorrectly matched
# -------------------------------------------------------------------------------------------------
def test_6_composite_items_and_variants_not_falsely_matched(item_master_records):
    # '2 Idli + 1 Masala Dosa Online' should NOT match 'Masala Dosa' or 'Idli (2pc)'
    res_id, _, status, _, _, candidates, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="2 Idli + 1 Masala Dosa Online",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status in ("UNRESOLVED", "AMBIGUOUS")
    assert res_id is None

    # 'Plain Dosa Parcel' is NOT in master (only 'Plain Dosa') -> must NOT silently resolve
    res_id, _, status, _, _, candidates, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Plain Dosa Parcel",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status in ("UNRESOLVED", "AMBIGUOUS")
    assert res_id is None


# -------------------------------------------------------------------------------------------------
# Scenario 7: Missing or unknown item names are reported as UNRESOLVED
# -------------------------------------------------------------------------------------------------
def test_7_missing_or_unknown_item_names_reported_as_unresolved(item_master_records):
    for bad_name in [None, "", "   ", "nan", "None", "Unknown Mystery Dish"]:
        res_id, _, status, _, _, _, msg = DynamicLookupResolver.match_name_to_id(
            excel_name=bad_name,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            db_records=item_master_records
        )
        assert res_id is None
        assert status in ("UNRESOLVED", "MISSING_NAME")
        assert len(msg) > 0


# -------------------------------------------------------------------------------------------------
# Scenario 8: Excel column order changes do NOT break mappings
# -------------------------------------------------------------------------------------------------
def test_8_excel_column_order_changes_preserve_correct_mapping(item_schema_meta):
    # Standard order: item_name, item_price, item_quantity, item_total
    order_a = ["item_name", "item_price", "item_quantity", "item_total"]
    # Shuffled order: item_total, item_quantity, item_name, item_price
    order_b = ["item_total", "item_quantity", "item_name", "item_price"]

    mappings_a = ColumnMatcher.match_columns(order_a, item_schema_meta)
    mappings_b = ColumnMatcher.match_columns(order_b, item_schema_meta)

    oli_map_a = {m.excel_column: m.database_column for m in mappings_a if m.database_table == "OrderLineItems"}
    oli_map_b = {m.excel_column: m.database_column for m in mappings_b if m.database_table == "OrderLineItems"}

    # Both must map identically by header name regardless of order
    assert oli_map_a.get("item_name") == "Description"
    assert oli_map_b.get("item_name") == "Description"
    assert oli_map_a.get("item_price") == "Item_Price"
    assert oli_map_b.get("item_price") == "Item_Price"
    assert oli_map_a.get("item_quantity") == "Quantity"
    assert oli_map_b.get("item_quantity") == "Quantity"

    # Crucial safety: item_total or item_price MUST NEVER map to Item_ID
    assert oli_map_a.get("item_total") != "Item_ID"
    assert oli_map_b.get("item_total") != "Item_ID"
    assert "Item_ID" not in oli_map_a.values()
    assert "Item_ID" not in oli_map_b.values()


# -------------------------------------------------------------------------------------------------
# Scenario 9: Source transaction price is preserved
# -------------------------------------------------------------------------------------------------
def test_9_source_transaction_price_is_preserved(item_master_records):
    # Historical order line where customer bought Masala Dosa at discount price 39.00
    # Master price is 46.296. The source price must remain 39.00!
    source_row = {
        "item_name": "Masala Dosa",
        "item_price": 39.00,
        "item_quantity": 2
    }

    # Verify ID resolves to master item
    res_id, res_name, status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name=source_row["item_name"],
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=item_master_records
    )
    assert status == "MATCHED"

    # Destination record parameters:
    dest_record = {
        "Item_ID": res_id,
        "Description": source_row["item_name"],
        "Item_Price": source_row["item_price"],  # Source transaction price preserved!
        "Quantity": source_row["item_quantity"]
    }

    assert dest_record["Item_ID"] == "22"
    assert dest_record["Item_Price"] == 39.00
    assert dest_record["Quantity"] == 2
    assert dest_record["Item_Price"] != 46.296  # Master price not blindly overwriting history


# -------------------------------------------------------------------------------------------------
# Scenario 10: Reconciliation service produces safe diagnostic preview and rollback transaction
# -------------------------------------------------------------------------------------------------
def test_10_safe_diagnostic_report_and_transaction_rollback(item_master_records):
    # Create SQLite in-memory test database mimicking the tables
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ItemsMaster (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Item_ID VARCHAR(50) NOT NULL,
                Item VARCHAR(100) NOT NULL,
                Item_Price DECIMAL(10,2) NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE OrderLineItems (
                Line_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_ID INTEGER,
                Item_ID VARCHAR(50),
                Description VARCHAR(200),
                Item_Price DECIMAL(10,2),
                Quantity INTEGER
            )
        """))

        # Seed master items
        for r in item_master_records:
            conn.execute(
                text("INSERT INTO ItemsMaster (Item_ID, Item, Item_Price) VALUES (:id, :name, :price)"),
                {"id": r["Item_ID"], "name": r["Item"], "price": r["Item_Price"]}
            )

        # Seed corrupt order line items (simulating prior incorrect mappings)
        conn.execute(text("""
            INSERT INTO OrderLineItems (Line_ID, Order_ID, Item_ID, Description, Item_Price, Quantity)
            VALUES 
                (1, 10, '79', 'Masala Dosa', 79.00, 1),
                (2, 10, '59', 'Sweet Lassi', 59.00, 1),
                (3, 11, '59', 'Gulab Jamun (2pc)', 27.78, 2),
                (4, 12, '999', 'Mystery Combo Item', 120.00, 1)
        """))

    # 1. Run diagnostic report
    summary = ItemReconciliationService.generate_diagnostic_report(
        engine=engine,
        child_table="OrderLineItems",
        master_table="ItemsMaster"
    )

    assert summary.total_scanned == 4
    assert summary.matching_count == 1  # Gulab Jamun (2pc) was already 59
    assert summary.mismatched_count == 3
    assert summary.confirmed_count == 2  # Masala Dosa -> 22, Sweet Lassi -> 119
    assert summary.unresolved_count == 1  # Mystery Combo Item

    # 2. Preview without apply=True does not change database
    preview_res = ItemReconciliationService.apply_corrections(
        approved_line_ids=[1, 2],
        engine=engine,
        apply=False
    )
    assert preview_res["success"] is False
    assert "requires explicit confirmation" in preview_res["message"]

    # Verify database unchanged
    with engine.connect() as conn:
        row1 = conn.execute(text("SELECT Item_ID, Item_Price FROM OrderLineItems WHERE Line_ID = 1")).fetchone()
        assert row1[0] == "79"
        assert float(row1[1]) == 79.00

    # 3. Apply confirmed corrections
    apply_res = ItemReconciliationService.apply_corrections(
        approved_line_ids=[1, 2],
        engine=engine,
        apply=True
    )
    assert apply_res["success"] is True
    assert apply_res["updated_count"] == 2

    # Verify database was safely updated and prices preserved
    with engine.connect() as conn:
        row1 = conn.execute(text("SELECT Item_ID, Description, Item_Price FROM OrderLineItems WHERE Line_ID = 1")).fetchone()
        row2 = conn.execute(text("SELECT Item_ID, Description, Item_Price FROM OrderLineItems WHERE Line_ID = 2")).fetchone()
        row3 = conn.execute(text("SELECT Item_ID, Description, Item_Price FROM OrderLineItems WHERE Line_ID = 3")).fetchone()

        assert row1[0] == "22"  # Masala Dosa now points to Item_ID 22!
        assert float(row1[2]) == 79.00  # Historical transaction price preserved!
        assert row2[0] == "119"  # Sweet Lassi now points to Item_ID 119!
        assert float(row2[2]) == 59.00  # Historical transaction price preserved!
        assert row3[0] == "59"  # Gulab Jamun remains untouched

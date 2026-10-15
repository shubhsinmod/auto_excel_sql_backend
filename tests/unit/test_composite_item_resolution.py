"""
Comprehensive Unit Tests for Dynamic Composite Matching for Item_ID Resolution.

Verifies:
1. Duplicate item names across different categories are resolved dynamically using Category.
2. Duplicate item names across different item groups are resolved dynamically using Item Group.
3. Disambiguation using separate classification master tables (e.g. Category_ID -> Categories).
4. Disambiguation using packaging variants (e.g. '2pc', 'Parcel', 'Online').
5. Missing or conflicting category/group attributes correctly result in AMBIGUOUS without guessing.
6. Identical prices across distinct items never cause cross-resolution (price independence).
7. Preservation of transaction prices and quantities during resolution and diagnostic reporting.
8. Read-only diagnostic report contains all required fields:
   (row_number, source_item_name, source_category/group, additional attributes, candidates, resolved ID, method, status).

Strict Requirements:
- Zero hardcoding of table names, column names, item names, or IDs.
- Zero database writes in diagnostic mode.
"""

import pytest
import pandas as pd
from typing import Dict, List, Any
from app.migration.lookup_resolver import DynamicLookupResolver
from app.migration.item_reconciliation import ItemReconciliationService


@pytest.fixture
def composite_item_master():
    """
    ItemsMaster containing duplicate names differentiated by category, group, variant, or linked id.
    For instance:
    - 'Chai': Beverages (ID 10) vs Snacks (ID 11)
    - 'Paneer Tikka': Starters (ID 20) vs Main Course (ID 21)
    - 'Masala Dosa': Dine-In (ID 30) vs Parcel (ID 31)
    - 'Gulab Jamun': 2pc (ID 40) vs 4pc (ID 41)
    """
    return [
        {
            "ID": 1,
            "Item_ID": "10",
            "Item": "Chai",
            "Category": "Beverages",
            "Item_Group": "Hot Drinks",
            "Item_Price": 20.00
        },
        {
            "ID": 2,
            "Item_ID": "11",
            "Item": "Chai",
            "Category": "Snacks",
            "Item_Group": "Bakery",
            "Item_Price": 25.00
        },
        {
            "ID": 3,
            "Item_ID": "20",
            "Item": "Paneer Tikka",
            "Category": "Starters",
            "Item_Group": "Tandoor",
            "Item_Price": 180.00
        },
        {
            "ID": 4,
            "Item_ID": "21",
            "Item": "Paneer Tikka",
            "Category": "Main Course",
            "Item_Group": "Gravy",
            "Item_Price": 220.00
        },
        {
            "ID": 5,
            "Item_ID": "30",
            "Item": "Masala Dosa",
            "Category": "South Indian",
            "Item_Group": "Dosa",
            "Packaging": "Dine In",
            "Item_Price": 60.00
        },
        {
            "ID": 6,
            "Item_ID": "31",
            "Item": "Masala Dosa",
            "Category": "South Indian",
            "Item_Group": "Dosa",
            "Packaging": "Parcel",
            "Item_Price": 70.00
        },
        {
            "ID": 7,
            "Item_ID": "40",
            "Item": "Gulab Jamun (2pc)",
            "Category": "Desserts",
            "Item_Group": "Sweets",
            "Item_Price": 40.00
        },
        {
            "ID": 8,
            "Item_ID": "41",
            "Item": "Gulab Jamun (4pc)",
            "Category": "Desserts",
            "Item_Group": "Sweets",
            "Item_Price": 75.00
        },
        # Items with linked Category_ID referencing a separate Categories table
        {
            "ID": 9,
            "Item_ID": "50",
            "Item": "Veg Biryani",
            "Category_ID": "CAT_RICE",
            "Item_Price": 150.00
        },
        {
            "ID": 10,
            "Item_ID": "51",
            "Item": "Veg Biryani",
            "Category_ID": "CAT_COMBO",
            "Item_Price": 190.00
        }
    ]


@pytest.fixture
def linked_classification_tables():
    """Separate classification tables for linked category foreign keys."""
    return {
        "Categories": [
            {"Category_ID": "CAT_RICE", "Category_Name": "Rice Bowls & Biryanis"},
            {"Category_ID": "CAT_COMBO", "Category_Name": "Combo Meals"}
        ]
    }


# =================================================================================================
# 1. Duplicate item names across different categories
# =================================================================================================
def test_duplicate_item_names_resolved_by_category(composite_item_master):
    # 'Chai' with category 'Beverages' should resolve to Item_ID 10
    row_data_bev = {"Item_Name": "Chai", "Category": "Beverages", "Price": 18.00}
    res_id, res_name, status, method, conf, _, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="Chai",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_bev
    )
    assert status == "MATCHED"
    assert res_id == "10"
    assert "CATEGORY" in method

    # 'Chai' with category 'Snacks' should resolve to Item_ID 11
    row_data_snk = {"Item_Name": "Chai", "Category": "Snacks", "Price": 24.00}
    res_id2, _, status2, method2, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Chai",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_snk
    )
    assert status2 == "MATCHED"
    assert res_id2 == "11"
    assert "CATEGORY" in method2


# =================================================================================================
# 2. Duplicate item names across different item groups
# =================================================================================================
def test_duplicate_item_names_resolved_by_item_group(composite_item_master):
    # 'Paneer Tikka' with item group 'Tandoor' -> Item_ID 20
    row_data_tandoor = {"item": "Paneer Tikka", "item_group": "Tandoor", "price": 180.00}
    res_id, _, status, method, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Paneer Tikka",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_tandoor
    )
    assert status == "MATCHED"
    assert res_id == "20"
    assert "GROUP" in method

    # 'Paneer Tikka' with item group 'Gravy' -> Item_ID 21
    row_data_gravy = {"item": "Paneer Tikka", "menu_group": "Gravy", "price": 220.00}
    res_id2, _, status2, method2, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Paneer Tikka",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_gravy
    )
    assert status2 == "MATCHED"
    assert res_id2 == "21"
    assert "GROUP" in method2


# =================================================================================================
# 3. Disambiguation via linked classification tables (Category_ID -> Categories)
# =================================================================================================
def test_disambiguation_via_linked_classification_table(composite_item_master, linked_classification_tables):
    # Excel row has Category = "Combo Meals"
    row_data = {"Description": "Veg Biryani", "Category": "Combo Meals", "Rate": 190.00}
    res_id, _, status, method, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Veg Biryani",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data,
        all_parent_records=linked_classification_tables
    )
    assert status == "MATCHED"
    assert res_id == "51"
    assert "CATEGORY" in method


# =================================================================================================
# 4. Packaging variants and composite attributes (e.g. '2pc', 'Parcel')
# =================================================================================================
def test_disambiguation_via_packaging_variant(composite_item_master):
    # 'Gulab Jamun (2pc)' vs 'Gulab Jamun (4pc)'
    row_data_2pc = {"Item": "Gulab Jamun", "Variant": "2pc", "Price": 40.00}
    res_id, _, status, method, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Gulab Jamun",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_2pc
    )
    assert status == "MATCHED"
    assert res_id == "40"

    # 'Masala Dosa' with parcel variant signal
    row_data_parcel = {"Description": "Masala Dosa", "Packaging": "Parcel", "Amount": 70.00}
    res_id2, _, status2, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Masala Dosa",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_parcel
    )
    assert status2 == "MATCHED"
    assert res_id2 == "31"


# =================================================================================================
# 5. Missing or conflicting classification attributes report AMBIGUOUS safely
# =================================================================================================
def test_missing_or_conflicting_category_reports_ambiguous(composite_item_master):
    # 'Chai' without any Category or Group in row_data -> must be AMBIGUOUS, never guess ID
    row_data_no_cat = {"Description": "Chai", "Rate": 22.00}
    res_id, _, status, method, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
        excel_name="Chai",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_no_cat
    )
    assert status == "AMBIGUOUS"
    assert res_id is None
    assert len(candidates) >= 2
    assert {"10", "11"}.issubset({str(c["id"]) for c in candidates})

    # 'Chai' with conflicting/unknown category 'Automotive' -> must remain AMBIGUOUS
    row_data_conflict = {"Description": "Chai", "Category": "Automotive", "Rate": 20.00}
    res_id2, _, status2, _, _, candidates2, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Chai",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data_conflict
    )
    assert status2 == "AMBIGUOUS"
    assert res_id2 is None


# =================================================================================================
# 6. Price Independence: identical price never determines item resolution
# =================================================================================================
def test_price_independence_never_guesses_item_from_price(composite_item_master):
    # Even if Chai has price 20 in DB and 25 in DB, an Excel transaction of 20.00 with snacks category
    # MUST resolve by snacks category to 11, NOT by price 20 to 10!
    row_data = {"Item": "Chai", "Category": "Snacks", "Price": 20.00}  # Price matches ID 10, but category matches ID 11!
    res_id, _, status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
        excel_name="Chai",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        row_data=row_data
    )
    assert status == "MATCHED"
    assert res_id == "11"  # Matched by category, NEVER overridden by price!


# =================================================================================================
# 7. Foreign key resolution in resolve_foreign_key_record preserves full row disambiguation
# =================================================================================================
def test_resolve_foreign_key_record_composite_disambiguation(composite_item_master):
    row_dict = {"item_name": "Paneer Tikka", "category": "Starters", "price": 175.00}
    res_rec = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=composite_item_master,
        excel_source_value="Paneer Tikka",
        row_data=row_dict
    )
    assert res_rec.status == "RESOLVED"
    assert str(res_rec.matched_primary_key_value) == "20"
    assert "CATEGORY" in res_rec.match_method


# =================================================================================================
# 8. Read-only diagnostic report contains all required fields and preserves transaction values
# =================================================================================================
def test_diagnostic_report_generation_with_composite_matching(composite_item_master):
    child_records = [
        {
            "Line_ID": 1001,
            "Order_ID": 501,
            "Description": "Chai",
            "Category": "Beverages",
            "Item_Price": 18.50,
            "Quantity": 3
        },
        {
            "Line_ID": 1002,
            "Order_ID": 502,
            "Description": "Chai",
            "Item_Price": 25.00,
            "Quantity": 1
            # Missing Category -> Ambiguous
        },
        {
            "Line_ID": 1003,
            "Order_ID": 503,
            "Description": "Unknown Mystery Dish",
            "Item_Price": 99.00,
            "Quantity": 2
            # Not in DB -> Unresolved
        }
    ]

    summary = ItemReconciliationService.generate_diagnostic_report(
        child_records=child_records,
        master_records=composite_item_master
    )

    assert summary.total_scanned == 3
    assert summary.confirmed_count == 1
    assert summary.ambiguous_count == 1
    assert summary.unresolved_count == 1

    # Inspect first report item (CONFIRMED)
    item1 = summary.report[0]
    assert item1.line_id == 1001
    assert item1.row_number == 1
    assert item1.description == "Chai"
    assert item1.source_category == "Beverages"
    assert item1.proposed_item_id == "10"
    assert item1.current_price == 18.50  # Transaction price preserved
    assert item1.quantity == 3  # Transaction quantity preserved
    assert item1.status == "CONFIRMED"
    assert "CATEGORY" in item1.match_method

    # Inspect second report item (AMBIGUOUS)
    item2 = summary.report[1]
    assert item2.line_id == 1002
    assert item2.row_number == 2
    assert item2.status == "AMBIGUOUS"
    assert len(item2.candidates) >= 2
    assert item2.current_price == 25.00

    # Inspect third report item (UNRESOLVED)
    item3 = summary.report[2]
    assert item3.line_id == 1003
    assert item3.row_number == 3
    assert item3.status == "UNRESOLVED"
    assert item3.proposed_item_id is None

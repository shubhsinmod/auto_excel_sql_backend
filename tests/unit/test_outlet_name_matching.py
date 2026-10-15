"""
Comprehensive tests for Outlet Name Column Mapping and Intelligent Outlet Matching.

Test Requirements Covered:
1. Database 'Namaste', Excel 'Namaste BTM' -> resolves to Namaste.
2. Database 'Namaste', Excel 'Namaste Hoodi' -> resolves to Namaste.
3. Database 'Namaste BTM', Excel 'Namaste BTM' -> exact branch match.
4. Database contains separate 'Namaste BTM' and 'Namaste Hoodi' outlets -> preserve branch identities.
5. 'restaurant_name' and 'virtual_brand_name' contain different values -> flag discrepancy for review.
6. No matching outlet exists -> marked UNRESOLVED with clear message.
7. Multiple possible database matches exist -> flagged as AMBIGUOUS_MATCH for user review.
8. Outlet ID is correctly propagated to dependent records (Order.Order_Outlet_ID, Bills.Outlet_ID, Customer.Outlet_ID).
"""

import pytest
import pandas as pd
from typing import Dict, Any, List

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult, ResolvedEntityRecord
from app.mapping.matcher import ColumnMatcher
from app.migration.outlet_matcher import OutletMatcher, extract_outlet_base_name
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship


@pytest.fixture
def sample_schema_with_outlets():
    """Builds schema with Outlets, Customer, Order, Bills."""
    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR", max_length=100, nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Address", type="VARCHAR", max_length=300, nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Short_Code", type="VARCHAR", max_length=20, nullable=True, primary_key=False, identity=False),
    ]
    outlets_tbl = TableMetadata(
        name="Outlets",
        schema_name="dbo",
        columns=outlets_cols,
        primary_keys=["Outlet_ID"],
        foreign_keys=[]
    )

    cust_fk_outlet = ForeignKeyInfo(
        constrained_column="Outlet_ID",
        target_table="Outlets",
        target_column="Outlet_ID",
        target_schema="dbo"
    )
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Location", type="VARCHAR", max_length=200, nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Mobile_Number", type="VARCHAR", max_length=50, nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, primary_key=False, identity=False, foreign_keys=[cust_fk_outlet]),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[cust_fk_outlet]
    )

    order_fk_outlet = ForeignKeyInfo(
        constrained_column="Order_Outlet_ID",
        target_table="Outlets",
        target_column="Outlet_ID",
        target_schema="dbo"
    )
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_Outlet_ID", type="INT", nullable=True, primary_key=False, identity=False, foreign_keys=[order_fk_outlet]),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False, primary_key=False, identity=False),
    ]
    order_tbl = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[order_fk_outlet]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        dialect="mssql",
        tables=[outlets_tbl, customer_tbl, order_tbl]
    )


def test_column_mapping_outlet_name_not_location(sample_schema_with_outlets):
    """
    Requirement 1:
    - restaurant_name and virtual_brand_name MUST map to Outlets.Outlet_Name.
    - They must NOT map to Customer.Location or any other location field.
    """
    excel_cols = ["restaurant_name", "virtual_brand_name"]
    mappings = ColumnMatcher.match_columns(excel_cols, sample_schema_with_outlets)
    res_map = {m.excel_column: m for m in mappings}

    assert res_map["restaurant_name"].status == "MATCHED"
    assert res_map["restaurant_name"].database_table == "Outlets"
    assert res_map["restaurant_name"].database_column == "Outlet_Name"
    assert res_map["restaurant_name"].database_column != "Location"

    assert res_map["virtual_brand_name"].status == "MATCHED"
    assert res_map["virtual_brand_name"].database_table == "Outlets"
    assert res_map["virtual_brand_name"].database_column == "Outlet_Name"
    assert res_map["virtual_brand_name"].database_column != "Location"


def test_case_1_db_namaste_excel_namaste_btm():
    """
    Case 1: Database 'Namaste', Excel 'Namaste BTM'.
    Expected: Resolves to base outlet 'Namaste' (ID=101), match_type=NORMALIZED_BASE_MATCH.
    """
    db_outlets = [
        {"Outlet_ID": 101, "Outlet_Name": "Namaste", "Address": "Bangalore", "Short_Code": "NAM"}
    ]
    res = OutletMatcher.match_outlet_name("Namaste BTM", db_outlets)

    assert res["status"] == "MATCHED"
    assert res["outlet_id"] == 101
    assert res["matched_outlet_name"] == "Namaste"
    assert res["normalized_base"] == "namaste"
    assert res["detected_locality"] == "BTM"
    assert res["match_type"] == "NORMALIZED_BASE_MATCH"


def test_case_2_db_namaste_excel_namaste_hoodi():
    """
    Case 2: Database 'Namaste', Excel 'Namaste Hoodi'.
    Expected: Resolves to base outlet 'Namaste' (ID=101), match_type=NORMALIZED_BASE_MATCH.
    """
    db_outlets = [
        {"Outlet_ID": 101, "Outlet_Name": "Namaste", "Address": "Bangalore", "Short_Code": "NAM"}
    ]
    res = OutletMatcher.match_outlet_name("Namaste Hoodi", db_outlets)

    assert res["status"] == "MATCHED"
    assert res["outlet_id"] == 101
    assert res["matched_outlet_name"] == "Namaste"
    assert res["normalized_base"] == "namaste"
    assert res["detected_locality"] == "Hoodi"
    assert res["match_type"] == "NORMALIZED_BASE_MATCH"


def test_case_3_db_namaste_btm_excel_namaste_btm():
    """
    Case 3: Database 'Namaste BTM', Excel 'Namaste BTM'.
    Expected: Exact match on 'Namaste BTM' (ID=201), match_type=EXACT_MATCH.
    """
    db_outlets = [
        {"Outlet_ID": 201, "Outlet_Name": "Namaste BTM", "Address": "BTM Layout, Bangalore", "Short_Code": "BTM"}
    ]
    res = OutletMatcher.match_outlet_name("Namaste BTM", db_outlets)

    assert res["status"] == "MATCHED"
    assert res["outlet_id"] == 201
    assert res["matched_outlet_name"] == "Namaste BTM"
    assert res["match_type"] == "EXACT_MATCH"


def test_case_4_separate_branches_in_database():
    """
    Case 4: Database contains separate 'Namaste BTM' (ID=301) and 'Namaste Hoodi' (ID=302) outlets.
    Expected:
    - Excel 'Namaste BTM' matches ID=301.
    - Excel 'Namaste Hoodi' matches ID=302.
    - Excel generic 'Namaste' without branch is flagged as AMBIGUOUS_MATCH because both branches exist.
    """
    db_outlets = [
        {"Outlet_ID": 301, "Outlet_Name": "Namaste BTM", "Address": "BTM Layout", "Short_Code": "NBTM"},
        {"Outlet_ID": 302, "Outlet_Name": "Namaste Hoodi", "Address": "Hoodi Circle", "Short_Code": "NHOD"}
    ]

    res_btm = OutletMatcher.match_outlet_name("Namaste BTM", db_outlets)
    assert res_btm["status"] == "MATCHED"
    assert res_btm["outlet_id"] == 301

    res_hoodi = OutletMatcher.match_outlet_name("Namaste Hoodi", db_outlets)
    assert res_hoodi["status"] == "MATCHED"
    assert res_hoodi["outlet_id"] == 302

    # Generic Namaste should be flagged as ambiguous between the two branches
    res_generic = OutletMatcher.match_outlet_name("Namaste", db_outlets)
    assert res_generic["status"] == "AMBIGUOUS"
    assert res_generic["match_type"] == "AMBIGUOUS_MATCH"
    assert len(res_generic["candidates"]) == 2


def test_case_5_differing_restaurant_name_and_virtual_brand():
    """
    Case 5: 'restaurant_name' and 'virtual_brand_name' contain different values resolving to different outlets.
    Expected: Flagged as DIFFERENT_OUTLETS_DETECTED / AMBIGUOUS for user review.
    """
    db_outlets = [
        {"Outlet_ID": 401, "Outlet_Name": "Leon Grill", "Address": "Indiranagar", "Short_Code": "LG"},
        {"Outlet_ID": 402, "Outlet_Name": "Burger Co", "Address": "Koramangala", "Short_Code": "BC"}
    ]
    row_data = {
        "restaurant_name": "Leon Grill",
        "virtual_brand_name": "Burger Co"
    }
    res = OutletMatcher.resolve_row_outlet(row_data, db_outlets)

    assert res["status"] == "CONFLICTING_OUTLET_VALUES"
    assert res["match_type"] == "CONFLICTING_OUTLET_VALUES"
    assert res["outlet_id"] is None
    assert "Discrepancy" in res["message"]


def test_branch_suffix_matches_unique_existing_outlet_name():
    db_outlets = [
        {"Outlet_ID": 1, "Outlet_Name": "BTM", "Address": "BTM Layout", "Short_Code": "BTM"}
    ]

    res = OutletMatcher.resolve_row_outlet(
        {"restaurant_name": "ANN - BTM", "virtual_brand_name": "ANN - BTM"},
        db_outlets
    )

    assert res["status"] == "MATCHED"
    assert res["outlet_id"] == 1
    assert res["match_type"].startswith("DUAL_COLUMN_MATCH (BRANCH_SUFFIX_MATCH)")


def test_branch_suffix_with_multiple_existing_ids_is_ambiguous():
    db_outlets = [
        {"Outlet_ID": 1, "Outlet_Name": "BTM", "Short_Code": "BTM"},
        {"Outlet_ID": 2, "Outlet_Name": "BTM Layout", "Short_Code": "BTM"}
    ]

    res = OutletMatcher.match_outlet_name("ANN - BTM", db_outlets)

    assert res["status"] == "AMBIGUOUS"
    assert res["match_type"] == "AMBIGUOUS_MATCH"
    assert {candidate["id"] for candidate in res["candidates"]} == {1, 2}


def test_case_6_no_matching_outlet_exists():
    """
    Case 6: No matching outlet exists in the database.
    Expected: Marked UNRESOLVED with clear message, confidence 0.0.
    """
    db_outlets = [
        {"Outlet_ID": 501, "Outlet_Name": "Leon Grill", "Address": "Indiranagar", "Short_Code": "LG"}
    ]
    res = OutletMatcher.match_outlet_name("NonExistent Biryani", db_outlets)

    assert res["status"] == "UNRESOLVED"
    assert res["match_type"] == "NO_MATCH"
    assert res["outlet_id"] is None
    assert "No existing database outlet found" in res["message"]


def test_case_7_multiple_possible_database_matches():
    """
    Case 7: Multiple possible database matches exist with identical base name.
    Expected: Flagged as AMBIGUOUS_MATCH for user review.
    """
    db_outlets = [
        {"Outlet_ID": 601, "Outlet_Name": "Namaste Express", "Address": "BTM", "Short_Code": "NE1"},
        {"Outlet_ID": 602, "Outlet_Name": "Namaste Express", "Address": "Hoodi", "Short_Code": "NE2"}
    ]
    res = OutletMatcher.match_outlet_name("Namaste Express", db_outlets)

    assert res["status"] == "AMBIGUOUS"
    assert res["match_type"] == "AMBIGUOUS_MATCH"
    assert res["outlet_id"] is None
    assert len(res["candidates"]) == 2


def test_case_8_outlet_id_propagation_to_dependent_records():
    """
    Case 8: Outlet ID is correctly propagated to dependent records (Order.Order_Outlet_ID, Customer.Outlet_ID).
    """
    db_outlets = [
        {"Outlet_ID": 777, "Outlet_Name": "Namaste", "Address": "Bangalore Central", "Short_Code": "NAM"}
    ]
    row_data = {
        "restaurant_name": "Namaste BTM",
        "customer_name": "John Doe",
        "order_date": "2026-10-10"
    }

    # Resolve foreign key for Order.Order_Outlet_ID
    order_fk_res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Order",
        child_column="Order_Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=db_outlets,
        excel_source_value="Namaste BTM",
        row_data=row_data
    )

    assert order_fk_res.status == "RESOLVED"
    assert order_fk_res.matched_primary_key_value == 777
    assert order_fk_res.matched_primary_key_column == "Outlet_ID"
    assert order_fk_res.matched_table == "Outlets"

    # Resolve foreign key for Customer.Outlet_ID using same row
    cust_fk_res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Customer",
        child_column="Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=db_outlets,
        excel_source_value="Namaste BTM",
        row_data=row_data
    )

    assert cust_fk_res.status == "RESOLVED"
    assert cust_fk_res.matched_primary_key_value == 777


def test_safe_brand_descriptor_distinction():
    """
    Safety Guard:
    'Namaste Cafe' vs 'Namaste': do NOT automatically assume they are the same outlet.
    'Cafe' is a brand descriptor, NOT a locality suffix.
    """
    base_name, loc = extract_outlet_base_name("Namaste Cafe")
    assert base_name == "Namaste Cafe"
    assert loc is None

    db_outlets = [
        {"Outlet_ID": 801, "Outlet_Name": "Namaste", "Address": "Bangalore", "Short_Code": "NAM"}
    ]
    res = OutletMatcher.match_outlet_name("Namaste Cafe", db_outlets)
    # Must NOT match Namaste!
    assert res["status"] == "UNRESOLVED"
    assert res["outlet_id"] is None

import pytest
import pandas as pd
from sqlalchemy import create_engine, text

from app.utils.normalization import normalize_cell_value, tokenize_cell_value
from app.mapping.values import ValueMatcher, value_registry
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import ColumnMappingResult, ValueOverrideItem
from app.migration.validator import MigrationValidator
from app.migration.preview import MigrationPreviewEngine
from app.migration.engine import sanitize_value


# -------------------------------------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------------------------------------
VALID_ORDER_TYPE_DB_VALUES = ["Dine In", "Take Away", "Home Delivery", "Parcel"]


@pytest.fixture
def order_type_schema():
    col_order_id = ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True)
    col_order_type = ColumnMetadata(name="Order_Type", type="VARCHAR", nullable=False, primary_key=False, max_length=50)
    col_total = ColumnMetadata(name="Total_Amount", type="DECIMAL", nullable=True, primary_key=False)

    tbl_order = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=[col_order_id, col_order_type, col_total],
        primary_keys=["Order_ID"],
        foreign_keys=[]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[tbl_order]
    )


# -------------------------------------------------------------------------------------------------
# TEST 1: Exact matches
# -------------------------------------------------------------------------------------------------
def test_1_exact_value_matches():
    """
    Test exact cell value matches where Excel string matches valid database column value exactly.
    """
    val, match_type, status, conf, expl, cands = ValueMatcher.match_single_value(
        excel_value="Dine In",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )

    assert status == "MATCHED"
    assert match_type == "EXACT"
    assert val == "Dine In"
    assert conf == 1.0
    assert "Exact match" in expl

    # Also test Parcel
    val_p, match_p, status_p, _, _, _ = ValueMatcher.match_single_value(
        excel_value="Parcel",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status_p == "MATCHED"
    assert match_p == "EXACT"
    assert val_p == "Parcel"


# -------------------------------------------------------------------------------------------------
# TEST 2: Normalized matches (capitalization, spaces, underscores, hyphens, punctuation)
# -------------------------------------------------------------------------------------------------
def test_2_normalized_value_matches():
    """
    Test normalized value matches:
    - Capitalization: 'TAKEAWAY', 'TakeAway' -> 'Take Away'
    - Spaces: 'Take Away' -> 'Take Away'
    - Hyphens: 'Take-Away', 'take-away' -> 'Take Away'
    - Underscores: 'take_away' -> 'Take Away'
    - PascalCase: 'HomeDelivery' -> 'Home Delivery'
    - Punctuation: 'Dine-In.' -> 'Dine In'
    """
    # 1. TakeAway -> Take Away
    val1, match1, status1, conf1, _, _ = ValueMatcher.match_single_value(
        excel_value="TakeAway",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status1 == "MATCHED"
    assert match1 in ("NORMALIZED", "SYNONYM")
    assert val1 == "Take Away"
    assert conf1 >= 0.90

    # 2. Take-Away -> Take Away
    val2, match2, status2, conf2, _, _ = ValueMatcher.match_single_value(
        excel_value="Take-Away",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status2 == "MATCHED"
    assert val2 == "Take Away"

    # 3. take_away -> Take Away
    val3, match3, status3, conf3, _, _ = ValueMatcher.match_single_value(
        excel_value="take_away",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status3 == "MATCHED"
    assert val3 == "Take Away"

    # 4. HomeDelivery -> Home Delivery
    val4, match4, status4, conf4, _, _ = ValueMatcher.match_single_value(
        excel_value="HomeDelivery",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status4 == "MATCHED"
    assert val4 == "Home Delivery"
    assert conf4 >= 0.90

    # 5. Home-Delivery -> Home Delivery
    val5, _, status5, _, _, _ = ValueMatcher.match_single_value(
        excel_value="Home-Delivery",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status5 == "MATCHED"
    assert val5 == "Home Delivery"


# -------------------------------------------------------------------------------------------------
# TEST 3: Configurable Synonyms
# -------------------------------------------------------------------------------------------------
def test_3_synonym_value_matches():
    """
    Test configurable synonyms:
    - 'Dinning' -> 'Dine In'
    - 'Dining' -> 'Dine In'
    - 'Table' -> 'Dine In'
    - 'Takeout' -> 'Take Away'
    - 'Carry Out' -> 'Take Away'
    - 'Door Delivery' -> 'Home Delivery'
    - 'Packing' -> 'Parcel'
    """
    # 1. Dinning -> Dine In
    val1, match1, status1, conf1, expl1, _ = ValueMatcher.match_single_value(
        excel_value="Dinning",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status1 == "MATCHED"
    assert match1 == "SYNONYM"
    assert val1 == "Dine In"
    assert conf1 >= 0.90
    assert "synonym" in expl1.lower()

    # 2. Dining -> Dine In
    val2, match2, status2, _, _, _ = ValueMatcher.match_single_value(
        excel_value="Dining",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status2 == "MATCHED"
    assert val2 == "Dine In"

    # 3. Takeout -> Take Away
    val3, _, status3, _, _, _ = ValueMatcher.match_single_value(
        excel_value="Takeout",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status3 == "MATCHED"
    assert val3 == "Take Away"

    # 4. Packing -> Parcel
    val4, _, status4, _, _, _ = ValueMatcher.match_single_value(
        excel_value="Packing",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )
    assert status4 == "MATCHED"
    assert val4 == "Parcel"


# -------------------------------------------------------------------------------------------------
# TEST 4: Ambiguous values (Delivery(Parcel) -> Flags for User Review)
# -------------------------------------------------------------------------------------------------
def test_4_ambiguous_value_delivery_parcel():
    """
    Requirement 5:
    For 'Delivery(Parcel)', do not automatically choose 'Parcel' or 'Home Delivery'
    if both are valid candidates. Flag it for user review with status AMBIGUOUS.
    """
    val, match_type, status, conf, expl, candidates = ValueMatcher.match_single_value(
        excel_value="Delivery(Parcel)",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,  # Contains both 'Home Delivery' and 'Parcel'
        target_table="Order",
        target_column="Order_Type"
    )

    # Must be marked AMBIGUOUS, target_value must NOT be automatically chosen
    assert status == "AMBIGUOUS"
    assert match_type == "AMBIGUOUS"
    assert val is None
    assert conf <= 0.60
    assert "ambiguous" in expl.lower()

    # Verify both candidates are presented
    cand_values = [c["value"] for c in candidates]
    assert "Parcel" in cand_values
    assert "Home Delivery" in cand_values


# -------------------------------------------------------------------------------------------------
# TEST 5: Unmatched values
# -------------------------------------------------------------------------------------------------
def test_5_unmatched_values():
    """
    Test arbitrary unknown values that do not match any database value or synonym.
    """
    val, match_type, status, conf, expl, cands = ValueMatcher.match_single_value(
        excel_value="DriveThruVIP",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type"
    )

    assert status == "UNMATCHED"
    assert match_type == "UNMATCHED"
    assert val is None
    assert conf == 0.0
    assert "could not be matched" in expl


# -------------------------------------------------------------------------------------------------
# TEST 6: Caching approved mappings scoped to target database column
# -------------------------------------------------------------------------------------------------
def test_6_cached_approved_mappings():
    """
    Requirement 8:
    Cache approved mappings for future imports, scoped to the target database column.
    Once 'Delivery(Parcel)' -> 'Parcel' is approved and cached for 'Order.Order_Type',
    future runs match it as APPROVED.
    """
    # 1. Clean previous cache for test
    value_registry.delete_cached_mapping("Order", "Order_Type", "Delivery(Parcel)", database_scope="BOPOS_Leons")

    # 2. Before approval: AMBIGUOUS
    cached = value_registry.get_approved_mappings("Order", "Order_Type", database_scope="BOPOS_Leons")
    val_before, match_before, status_before, _, _, _ = ValueMatcher.match_single_value(
        excel_value="Delivery(Parcel)",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type",
        cached_mappings=cached,
        database_scope="BOPOS_Leons"
    )
    assert status_before == "AMBIGUOUS"

    # 3. Save approved mapping to column-scoped cache
    value_registry.save_approved_mapping(
        table="Order",
        column="Order_Type",
        source_value="Delivery(Parcel)",
        target_value="Parcel",
        database_scope="BOPOS_Leons"
    )

    # 4. After approval: Matches via APPROVED cache with confidence 1.0
    cached_after = value_registry.get_approved_mappings("Order", "Order_Type", database_scope="BOPOS_Leons")
    assert "Delivery(Parcel)" in cached_after
    assert cached_after["Delivery(Parcel)"] == "Parcel"

    val_after, match_after, status_after, conf_after, expl_after, _ = ValueMatcher.match_single_value(
        excel_value="Delivery(Parcel)",
        valid_db_values=VALID_ORDER_TYPE_DB_VALUES,
        target_table="Order",
        target_column="Order_Type",
        cached_mappings=cached_after,
        database_scope="BOPOS_Leons"
    )

    assert status_after == "MATCHED"
    assert match_after == "APPROVED"
    assert val_after == "Parcel"
    assert conf_after == 1.0
    assert "cached approved mapping" in expl_after

    # Clean up test cache
    value_registry.delete_cached_mapping("Order", "Order_Type", "Delivery(Parcel)", database_scope="BOPOS_Leons")


# -------------------------------------------------------------------------------------------------
# TEST 7: Applying approved mappings to rows during preview and SQL generation
# -------------------------------------------------------------------------------------------------
def test_7_apply_approved_mappings_to_rows(order_type_schema):
    """
    Requirement 7:
    Apply approved mappings to all matching rows during preview and SQL generation.
    """
    df = pd.DataFrame({
        "order_id": [101, 102, 103, 104],
        "order_type": ["Dinning", "TakeAway", "HomeDelivery", "Dine In"],
        "total": [250.0, 180.0, 420.0, 310.0]
    })

    col_mappings = [
        ColumnMappingResult(excelColumn="order_id", databaseTable="Order", databaseColumn="Order_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_type", databaseTable="Order", databaseColumn="Order_Type", status="MATCHED"),
        ColumnMappingResult(excelColumn="total", databaseTable="Order", databaseColumn="Total_Amount", status="MATCHED")
    ]

    engine = create_engine("sqlite:///:memory:")

    # Generate preview
    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_orders.xlsx",
        schema=order_type_schema,
        column_mappings=col_mappings,
        engine=engine
    )

    preview_rows = preview_res.preview_rows
    # Filter preview items for Order_Type column
    type_items = [p for p in preview_rows if p.target_column == "Order_Type"]

    assert len(type_items) == 4

    # Row 1: 'Dinning' -> 'Dine In' via VALUE_MAPPING
    row1 = type_items[0]
    assert row1.sourceValue == "Dinning"
    assert row1.finalValue == "Dine In"
    assert row1.valueSource == "VALUE_MAPPING"

    # Row 2: 'TakeAway' -> 'Take Away' via VALUE_MAPPING
    row2 = type_items[1]
    assert row2.sourceValue == "TakeAway"
    assert row2.finalValue == "Take Away"
    assert row2.valueSource == "VALUE_MAPPING"

    # Row 3: 'HomeDelivery' -> 'Home Delivery' via VALUE_MAPPING
    row3 = type_items[2]
    assert row3.sourceValue == "HomeDelivery"
    assert row3.finalValue == "Home Delivery"
    assert row3.valueSource == "VALUE_MAPPING"

    # Row 4: 'Dine In' -> 'Dine In' via EXCEL (exact)
    row4 = type_items[3]
    assert row4.sourceValue == "Dine In"
    assert row4.finalValue == "Dine In"
    assert row4.valueSource == "EXCEL"


# -------------------------------------------------------------------------------------------------
# TEST 8: Validation of mapped values against database constraints before SQL generation
# -------------------------------------------------------------------------------------------------
def test_8_validation_blocks_unresolved_ambiguous_on_required_column(order_type_schema):
    """
    Requirement 9:
    Validate all mapped values against database constraints before generating SQL.
    If 'Order_Type' is non-nullable (required) and has an unresolved ambiguous value,
    validation flags an ERROR and blocks execution (canExecute = False).
    """
    df = pd.DataFrame({
        "order_id": [1, 2],
        "order_type": ["Dine In", "Delivery(Parcel)"],
        "total": [100.0, 200.0]
    })

    col_mappings = [
        ColumnMappingResult(excelColumn="order_id", databaseTable="Order", databaseColumn="Order_ID", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_type", databaseTable="Order", databaseColumn="Order_Type", status="MATCHED"),
        ColumnMappingResult(excelColumn="total", databaseTable="Order", databaseColumn="Total_Amount", status="MATCHED")
    ]

    engine = create_engine("sqlite:///:memory:")

    # 1. Preview without user override for ambiguous value
    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="ambiguous_order.xlsx",
        schema=order_type_schema,
        column_mappings=col_mappings,
        engine=engine
    )

    val_summary = preview_res.validation_summary
    assert val_summary is not None
    # 'Order_Type' is required, so unresolved AMBIGUOUS value must block execution
    assert val_summary.can_execute is False
    assert any(e.error_type == "AMBIGUOUS_VALUE_MAPPING" for e in val_summary.errors)

    # 2. Preview WITH user override resolving 'Delivery(Parcel)' -> 'Parcel'
    user_val_overrides = {
        "Order.Order_Type": {
            "Delivery(Parcel)": "Parcel"
        }
    }

    preview_res_resolved = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="ambiguous_order.xlsx",
        schema=order_type_schema,
        column_mappings=col_mappings,
        engine=engine,
        user_value_overrides=user_val_overrides
    )

    val_summary_resolved = preview_res_resolved.validation_summary
    assert val_summary_resolved is not None
    assert val_summary_resolved.can_execute is True
    assert not any(e.error_type == "AMBIGUOUS_VALUE_MAPPING" for e in val_summary_resolved.errors)


# -------------------------------------------------------------------------------------------------
# TEST 9: Configurable Value Synonyms CRUD
# -------------------------------------------------------------------------------------------------
def test_9_value_synonyms_crud():
    """
    Test adding, querying, and deleting value synonyms dynamically in ValueSynonymRegistry.
    """
    category = "Order Type"
    target = "Dine In"
    custom_syn = "EatInsideRestaurant"

    # Add synonym
    value_registry.add_synonym(category, target, custom_syn)
    assert value_registry.find_synonym_target(custom_syn) == "Dine In"

    # Delete synonym
    value_registry.remove_synonym(category, target, custom_syn)
    assert value_registry.find_synonym_target(custom_syn) is None

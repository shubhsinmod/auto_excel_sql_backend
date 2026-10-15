from app.migration.lookup_resolver import DynamicLookupResolver


def resolve(name, master, row_data=None):
    return DynamicLookupResolver.match_name_to_id(
        excel_name=name, parent_table="ItemsMaster", parent_id_column="Item_ID",
        parent_name_column="Item", db_records=master, row_data=row_data or {"item_name": name},
    )


def test_idli_2pc_matches_database_double_when_category_matches():
    master = [
        {"ID": 1, "Item_ID": "101", "Item": "Idli (double)", "Category": "South Indian", "Item_Group": "Idli", "Item_Price": 40},
        {"ID": 2, "Item_ID": "102", "Item": "Vada (2pc)", "Category": "Snacks", "Item_Group": "Fried Snacks", "Item_Price": 40},
    ]
    result = resolve("Idli (2pc)", master, {"item_name": "Idli (2pc)", "category_name": "South Indian"})
    assert result[2] == "MATCHED"
    assert result[0] == "101"
    assert result[3] == "SEMANTIC_VARIANT_ALIAS"


def test_unique_item_variant_can_match_when_category_taxonomy_differs():
    # Source category may be a channel/report category while ItemsMaster uses a menu group.
    master = [{"Item_ID": "101", "Item": "Idli (double)", "Item_Group": "Thindi"}]
    result = resolve("Idli (2pc)", master, {"item_name": "Idli (2pc)", "category_name": "South Indian"})
    assert result[2] == "MATCHED"
    assert result[0] == "101"


def test_category_disambiguates_duplicate_semantic_items():
    master = [
        {"Item_ID": "101", "Item": "Idli (double)", "Category": "Breakfast"},
        {"Item_ID": "102", "Item": "Idli (double)", "Category": "South Indian"},
    ]
    result = resolve("Idli (2pc)", master, {"item_name": "Idli (2pc)", "category_name": "South Indian"})
    assert result[2] == "MATCHED"
    assert result[0] == "102"


def test_variant_alias_does_not_guess_when_multiple_candidates_remain():
    master = [
        {"Item_ID": "101", "Item": "Idli (double)", "Category": "Breakfast"},
        {"Item_ID": "102", "Item": "Idli (double)", "Category": "Snacks"},
    ]
    result = resolve("Idli (2pc)", master, {"item_name": "Idli (2pc)"})
    assert result[2] == "AMBIGUOUS"
    assert result[0] is None


def test_online_suffix_does_not_fall_back_to_neutral_base_item():
    master = [
        {"Item_ID": "201", "Item": "Masala Dosa", "Category": "South Indian"},
        {"Item_ID": "202", "Item": "Masala Dosa Parcel", "Category": "Thindi Parcel"},
    ]
    result = resolve("Masala Dosa Online", master, {"item_name": "Masala Dosa Online", "category_name": "South Indian"})
    assert result[2] in {"UNRESOLVED", "AMBIGUOUS"}
    assert result[0] is None


def test_parcel_suffix_does_not_fall_back_to_neutral_base_item():
    master = [{"Item_ID": "201", "Item": "Plain Dosa", "Category": "South Indian"}]
    result = resolve("Plain Dosa Parcel", master, {"item_name": "Plain Dosa Parcel", "category_name": "South Indian"})
    assert result[2] in {"UNRESOLVED", "AMBIGUOUS"}
    assert result[0] is None


def test_spelling_alias_bisi_bele_bath():
    master = [{"Item_ID": "301", "Item": "Bisibele Bhath", "Category": "Thindi"}]
    result = resolve("Bisi Bele Bath", master, {"item_name": "Bisi Bele Bath", "category_name": "South Indian"})
    assert result[2] == "MATCHED"
    assert result[0] == "301"


def test_single_and_double_portions_are_not_interchangeable():
    master = [{"Item_ID": "401", "Item": "Idli (double)", "Category": "Thindi"}]
    result = resolve("Idli (single)", master, {"item_name": "Idli (single)", "category_name": "Thindi"})
    assert result[2] in {"UNRESOLVED", "AMBIGUOUS"}
    assert result[0] is None


def test_item_price_disambiguates_semantic_alias_candidates():
    master = [
        {"Item_ID": "501", "Item": "Idli (double)", "Item_Group": "Thindi", "Item_Price": 40},
        {"Item_ID": "502", "Item": "Idli (double)", "Item_Group": "Thindi", "Item_Price": 50},
    ]
    result = resolve(
        "Idli (2pc)", master,
        {"item_name": "Idli (2pc)", "category_name": "Thindi", "item_price": 50},
    )
    assert result[2] == "MATCHED"
    assert result[0] == "502"


def test_item_price_mismatch_does_not_block_unique_semantic_alias():
    # OrderLineItems.Item_Price can be transaction/extended price, not master menu price.
    # A unique explicit alias plus compatible group must not become NULL because prices differ.
    master = [
        {"Item_ID": "501", "Item": "Idli (double)", "Item_Group": "Thindi", "Item_Price": 40},
    ]
    result = resolve(
        "Idli (2pc)", master,
        {"item_name": "Idli (2pc)", "category_name": "Thindi", "item_price": 50},
    )
    assert result[2] == "MATCHED"
    assert result[0] == "501"



def test_actual_fk_resolver_uses_explicit_item_variant_alias():
    # Exercise the resolver used by MigrationEngine, not only match_name_to_id.
    master = [
        {"Item_ID": 19, "Item": "Idli (double)", "Item_Group": "Thindi"},
        {"Item_ID": 40, "Item": "Idli (2pc) Online", "Item_Group": "South Indian"},
        {"Item_ID": 30, "Item": "Idli (2pc) Parcel", "Item_Group": "Thindi Parcel"},
    ]
    result = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=master,
        excel_source_value="Idli (2pc)",
        row_data={"Description": "Idli (2pc)", "category_name": "Thindi"},
    )
    assert result.status == "RESOLVED"
    assert result.matched_primary_key_value == 19
    assert result.match_method == "SEMANTIC_VARIANT_ALIAS"


def test_actual_fk_resolver_keeps_exact_channel_sku():
    master = [
        {"Item_ID": 19, "Item": "Idli (double)", "Item_Group": "Thindi"},
        {"Item_ID": 40, "Item": "Idli (2pc) Online", "Item_Group": "South Indian"},
    ]
    result = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="OrderLineItems",
        child_column="Item_ID",
        parent_table="ItemsMaster",
        parent_id_column="Item_ID",
        parent_name_column="Item",
        db_records=master,
        excel_source_value="Idli (2pc) Online",
        row_data={"Description": "Idli (2pc) Online"},
    )
    assert result.status == "RESOLVED"
    assert result.matched_primary_key_value == 40

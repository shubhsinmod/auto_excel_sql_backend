import re
import math
from typing import Dict, List, Any, Optional, Set, Tuple
import pandas as pd
from sqlalchemy import text, Engine
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import (
    ColumnMappingResult,
    ValueResolutionResult,
    ForeignKeyPlanItem,
    ResolvedEntityRecord,
    ValueMappingItem
)
from app.utils.normalization import (
    normalize_column_name,
    normalize_cell_value,
    tokenize_cell_value,
    normalize_string,
    stem_token
)
from app.mapping.synonyms import synonym_registry
from app.migration.outlet_matcher import OutletMatcher, outlet_row_source_key, outlet_row_source_values
from app.core.logging import logger
from app.migration.missing_value_resolver import identify_table_lookup_columns


def is_empty_or_na(val: Any) -> bool:
    """Checks if a cell value is null, NaN, NaT, or empty/whitespace."""
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return True
    if pd.isna(val):
        return True
    s = str(val).strip()
    return s == "" or s.lower() in ("nan", "none", "null", "nat")


def is_item_master_target(parent_table: str, child_table: str, child_column: str) -> bool:
    """Return True if the foreign key points from an order lines table to an items master table."""
    p_clean = parent_table.lower().split(".")[-1].replace("_", "").replace(" ", "")
    c_clean = child_column.lower().replace("_", "").replace(" ", "")
    ch_clean = child_table.lower().split(".")[-1].replace("_", "").replace(" ", "")
    is_item_parent = any(k in p_clean for k in ("item", "product", "menuitem", "dish"))
    is_item_col = any(k in c_clean for k in ("itemid", "item", "productid", "product", "menuid"))
    is_line_child = any(k in ch_clean for k in ("orderline", "orderitem", "lineitem", "orderdetail"))
    return (is_item_parent and is_item_col) or (is_line_child and is_item_col)


_ITEM_KEY_SET = {"description", "item", "itemname", "productname", "product", "menuitem", "dish"}
_CAT_KEY_SET = {"category", "catname", "categoryname", "subcategory", "itemcategory"}
_GRP_KEY_SET = {"itemgroup", "groupname", "menugroup", "group", "section", "dept"}
_DISCOVERED_COLS: Dict[Tuple[str, ...], Tuple[Optional[str], Optional[str], Optional[str]]] = {}


def item_row_source_key(row_data: Optional[Dict[str, Any]], s_val: Optional[str] = None) -> Tuple[str, str, str]:
    """Extract normalized (name, category, group) from row data for item lookup caching with fast-path lookup."""
    name_norm = normalize_cell_value(s_val or "") if s_val else ""
    cat_norm = ""
    grp_norm = ""
    if isinstance(row_data, dict) and row_data:
        keys_tuple = tuple(row_data.keys())
        discovered = _DISCOVERED_COLS.get(keys_tuple)
        if discovered is None:
            item_col = None
            cat_col = None
            grp_col = None
            for k in keys_tuple:
                k_clean = str(k).lower().replace("_", "").replace(" ", "")
                if not item_col and k_clean in _ITEM_KEY_SET:
                    item_col = k
                elif not cat_col and k_clean in _CAT_KEY_SET:
                    cat_col = k
                elif not grp_col and k_clean in _GRP_KEY_SET:
                    grp_col = k
            discovered = (item_col, cat_col, grp_col)
            _DISCOVERED_COLS[keys_tuple] = discovered

        item_col, cat_col, grp_col = discovered
        if not name_norm and item_col:
            v = row_data.get(item_col)
            if v is not None and not is_empty_or_na(v):
                name_norm = normalize_cell_value(str(v).strip())
        if cat_col:
            v = row_data.get(cat_col)
            if v is not None and not is_empty_or_na(v):
                cat_norm = normalize_cell_value(str(v).strip())
        if grp_col:
            v = row_data.get(grp_col)
            if v is not None and not is_empty_or_na(v):
                grp_norm = normalize_cell_value(str(v).strip())

    return (name_norm, cat_norm, grp_norm)


def extract_master_item_price(master_record: Optional[Dict[str, Any]]) -> Optional[float]:
    """Extract numeric Item_Price from master item record."""
    if not isinstance(master_record, dict):
        return None
    for key in ("Item_Price", "item_price", "Price", "price", "Unit_Price", "unit_price", "Rate", "rate"):
        if key in master_record and not is_empty_or_na(master_record[key]):
            try:
                return float(str(master_record[key]).replace(",", "").replace("₹", "").strip())
            except (ValueError, TypeError):
                continue
    return None


def find_item_price_column(columns: Any) -> Optional[str]:
    """Finds the price column name in a table's columns (list of ColumnMetadata or strings or dict)."""
    if isinstance(columns, dict):
        col_names = list(columns.keys())
    elif isinstance(columns, (list, set, tuple)):
        col_names = [c.name if hasattr(c, "name") else str(c) for c in columns]
    else:
        return None

    candidates = ("Item_Price", "item_price", "Price", "price", "Unit_Price", "unit_price", "Rate", "rate")
    for cand in candidates:
        for name in col_names:
            if name.lower().replace("_", "").replace(" ", "") == cand.lower().replace("_", "").replace(" ", ""):
                return name
    return None


def quote_identifier(name: str, dialect_name: str) -> str:
    """Quotes table or column identifiers appropriately for SQL Server, MySQL, Postgres, SQLite."""
    clean = str(name).strip().replace("[", "").replace("]", "").replace("`", "").replace('"', '')
    if dialect_name == "mssql":
        return f"[{clean}]"
    elif dialect_name in ("mysql", "mariadb"):
        return f"`{clean}`"
    else:
        return f'"{clean}"'


class LookupRelationship:
    """
    Represents an introspected or inferred foreign-key relationship between a child table's
    foreign-key column and a parent table's primary key, along with the identifying business-name column.
    """
    def __init__(
        self,
        child_table: str,
        child_column: str,
        parent_table: str,
        parent_id_column: str,
        parent_name_column: Optional[str] = None,
        excel_name_column: Optional[str] = None,
        excel_id_column: Optional[str] = None,
        is_required: bool = False
    ):
        self.child_table = child_table
        self.child_column = child_column
        self.parent_table = parent_table
        self.parent_id_column = parent_id_column
        self.parent_name_column = parent_name_column
        self.excel_name_column = excel_name_column
        self.excel_id_column = excel_id_column
        self.is_required = is_required

    def __repr__(self):
        return (
            f"<LookupRelationship {self.child_table}.{self.child_column} -> "
            f"{self.parent_table}.{self.parent_id_column} (name_col={self.parent_name_column}, "
            f"excel_name={self.excel_name_column})>"
        )


class TableLookupIndex:
    """
    High-performance pre-indexed hash lookup for parent table records.
    Provides O(1) exact, normalized, and override lookups, replacing O(N*M) nested loops.
    Retains the full database record dictionary for downstream entity reuse.
    """
    def __init__(self, db_records: List[Dict[str, Any]], parent_id_col: str, parent_name_col: str):
        self.exact_map: Dict[str, List[Dict[str, Any]]] = {}
        self.norm_map: Dict[str, List[Dict[str, Any]]] = {}
        self.override_map: Dict[str, Dict[str, Any]] = {}
        self.token_map: Dict[str, List[Dict[str, Any]]] = {}
        self.records_by_id: Dict[Any, Dict[str, Any]] = {}

        for r in db_records:
            raw_db_val = r.get(parent_name_col)
            db_id = r.get(parent_id_col)
            name_str = str(raw_db_val).strip() if raw_db_val is not None else ""

            if db_id is not None:
                self.records_by_id[db_id] = r
                self.records_by_id[str(db_id).strip()] = r
                self.override_map[str(db_id).strip()] = {
                    "id": db_id,
                    "name": name_str,
                    "record": r
                }

            cand_entry = {
                "id": db_id,
                "name": name_str if name_str else (str(db_id) if db_id is not None else ""),
                "record": r
            }

            # Collect all identifying text values for this record
            identifying_values: List[str] = []
            if name_str and name_str.lower() not in ("nan", "none", "null", "nat"):
                identifying_values.append(name_str)

            for col_k, col_v in r.items():
                if col_v is None or col_k == parent_name_col:
                    continue
                k_lower = col_k.lower()
                is_classification = any(c in k_lower for c in ("group", "category", "section", "style", "mis", "type", "sort", "status", "class"))
                is_ident_col = (
                    not is_classification
                    and (
                        "name" in k_lower
                        or "code" in k_lower
                        or "sku" in k_lower
                        or "address" in k_lower
                        or "location" in k_lower
                        or "title" in k_lower
                    )
                )
                if is_ident_col:
                    s = str(col_v).strip()
                    if s and s.lower() not in ("nan", "none", "null", "nat") and s not in identifying_values:
                        identifying_values.append(s)

            for s_val in identifying_values:
                # Exact map
                self.exact_map.setdefault(s_val, []).append(cand_entry)

                # Norm map
                norm = normalize_cell_value(s_val)
                if norm:
                    self.norm_map.setdefault(norm, []).append(cand_entry)
                    lower = s_val.lower()
                    if lower != norm:
                        self.norm_map.setdefault(lower, []).append(cand_entry)

                    # Token index for fast fuzzy candidate retrieval
                    for tok in norm.split():
                        if len(tok) >= 3:
                            c_list = self.token_map.setdefault(tok, [])
                            if len(c_list) < 5:
                                c_list.append(cand_entry)


def get_single_outlet_id(
    parent_records_by_table: Dict[str, List[Dict[str, Any]]],
    parent_table: str,
    parent_id_column: str
) -> Any:
    """Return the only existing outlet ID from already-loaded database records.

    Outlets are a read-only reference table in this migration. The configured schema
    metadata/lookup loader supplies the records; this helper never queries or writes
    the database and never matches an Excel outlet name.
    """
    requested = str(parent_table or "").strip().lower().split(".")[-1]
    if requested != "outlets":
        raise ValueError(f"'{parent_table}' is not an Outlets reference table.")

    records = parent_records_by_table.get(parent_table)
    if records is None:
        for key, value in parent_records_by_table.items():
            if str(key).strip().lower().split(".")[-1] == "outlets":
                records = value
                break
    records = records or []

    # A single record is required by the application's current business assumption.
    if len(records) != 1:
        raise ValueError(
            f"Expected exactly one record in outlet reference table '{parent_table}', "
            f"but found {len(records)}. No outlet ID was selected."
        )

    record = records[0]
    outlet_id = record.get(parent_id_column)
    if outlet_id is None or str(outlet_id).strip() == "":
        # Be tolerant of column casing from database drivers/schema metadata.
        outlet_id = next(
            (v for k, v in record.items() if str(k).lower() == str(parent_id_column).lower()),
            None
        )
    if outlet_id is None or str(outlet_id).strip() == "":
        raise ValueError(
            f"Outlet reference record in '{parent_table}' has no valid '{parent_id_column}'."
        )
    return outlet_id


class DynamicLookupResolver:
    """
    Dynamic Name-to-ID Resolution Engine for Microsoft SQL Server.
    Resolves missing foreign-key IDs by looking up business names from Excel against
    the referenced database tables dynamically using database metadata and SQL Server queries.
    Never hardcodes IDs, table mappings, or sample records.
    """

    _index_cache: Dict[Tuple[Any, ...], TableLookupIndex] = {}

    @classmethod
    def _make_index_key(
        cls,
        db_records: List[Dict[str, Any]],
        parent_id_column: str,
        parent_name_column: str
    ) -> Tuple[Any, ...]:
        sample = tuple(
            (str(r.get(parent_id_column)), str(r.get(parent_name_column)))
            for r in db_records[:50]
        )
        return (len(db_records), parent_id_column, parent_name_column, sample)

    @classmethod
    def get_or_create_index(
        cls,
        db_records: List[Dict[str, Any]],
        parent_id_column: str,
        parent_name_column: str
    ) -> TableLookupIndex:
        cache_key = cls._make_index_key(db_records, parent_id_column, parent_name_column)
        if cache_key not in cls._index_cache:
            cls._index_cache[cache_key] = TableLookupIndex(db_records, parent_id_column, parent_name_column)
        return cls._index_cache[cache_key]

    @classmethod
    def clear_index_cache(cls):
        cls._index_cache.clear()

    @staticmethod
    def discover_source_classification_values(row_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Dynamically discovers classification and descriptive values from source row_data.
        Finds category, group, code, variant, and description fields without hardcoding column names.
        """
        if not row_data or not isinstance(row_data, dict):
            return {
                "categories": [],
                "groups": [],
                "codes": [],
                "variants": [],
                "descriptions": [],
                "all_attributes": {}
            }

        found: Dict[str, Any] = {
            "categories": [],
            "groups": [],
            "codes": [],
            "variants": [],
            "descriptions": [],
            "all_attributes": {}
        }

        # Keywords for categorization
        CAT_KEYS = ("category", "cat_name", "category_name", "sub_category", "subcategory", "item_category")
        GRP_KEYS = ("item_group", "itemgroup", "group_name", "menu_group", "menugroup", "group", "section", "dept", "department")
        CODE_KEYS = ("item_code", "itemcode", "code", "sku", "barcode", "product_code", "hsn")
        VAR_KEYS = ("variant", "packaging", "portion", "size", "unit", "pack", "type")

        for k, v in row_data.items():
            if is_empty_or_na(v):
                continue
            k_lower = str(k).lower().strip()
            k_norm = normalize_column_name(str(k))
            val_str = str(v).strip()

            found["all_attributes"][k] = val_str

            if any(ck in k_lower or ck in k_norm for ck in CAT_KEYS):
                found["categories"].append((k, val_str))
            elif any(gk in k_lower or gk in k_norm for gk in GRP_KEYS):
                found["groups"].append((k, val_str))
            elif any(cdk in k_lower or cdk in k_norm for cdk in CODE_KEYS):
                found["codes"].append((k, val_str))
            elif any(vk in k_lower or vk in k_norm for vk in VAR_KEYS):
                found["variants"].append((k, val_str))
            elif "desc" in k_lower:
                found["descriptions"].append((k, val_str))

        return found

    @staticmethod
    def get_record_classification_values(
        rec: Dict[str, Any],
        all_parent_records: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> Dict[str, List[str]]:
        """
        Dynamically extracts classification strings from a database candidate record.
        Also resolves foreign-key references to classification tables (e.g. Category_ID -> Categories).
        """
        vals: Dict[str, List[str]] = {
            "category": [],
            "group": [],
            "code": [],
            "variant": [],
            "other": []
        }

        if not rec or not isinstance(rec, dict):
            return vals

        CAT_KEYS = ("category", "cat_name", "category_name", "sub_category", "subcategory")
        GRP_KEYS = ("group", "item_group", "menu_group", "section", "department")
        CODE_KEYS = ("code", "item_code", "sku", "barcode")

        for col_name, val in rec.items():
            if is_empty_or_na(val):
                continue
            col_l = col_name.lower().strip()
            val_str = str(val).strip()

            if any(ck in col_l for ck in CAT_KEYS):
                vals["category"].append(val_str)
            elif any(gk in col_l for gk in GRP_KEYS):
                vals["group"].append(val_str)
            elif any(cdk in col_l for cdk in CODE_KEYS):
                vals["code"].append(val_str)

        # Linked classification table resolution
        if all_parent_records:
            for col_name, val in rec.items():
                if is_empty_or_na(val):
                    continue
                col_l = col_name.lower().strip()
                val_str = str(val).strip()

                if "category" in col_l and (col_l.endswith("_id") or col_l.endswith("id")):
                    for tbl_name, tbl_recs in all_parent_records.items():
                        if "category" in tbl_name.lower() or "cat" in tbl_name.lower():
                            for cr in tbl_recs:
                                if any(str(cr.get(pk, "")).strip() == val_str for pk in cr if "id" in pk.lower()):
                                    for cn, cv in cr.items():
                                        if cv is not None and not is_empty_or_na(cv) and any(n in cn.lower() for n in ("name", "title", "desc")):
                                            vals["category"].append(str(cv).strip())

                elif "group" in col_l and (col_l.endswith("_id") or col_l.endswith("id")):
                    for tbl_name, tbl_recs in all_parent_records.items():
                        if "group" in tbl_name.lower():
                            for gr in tbl_recs:
                                if any(str(gr.get(pk, "")).strip() == val_str for pk in gr if "id" in pk.lower()):
                                    for gn, gv in gr.items():
                                        if gv is not None and not is_empty_or_na(gv) and any(n in gn.lower() for n in ("name", "title", "desc")):
                                            vals["group"].append(str(gv).strip())

        return vals

    @classmethod
    def disambiguate_candidates_by_composite_attributes(
        cls,
        candidates: List[Dict[str, Any]],
        row_data: Optional[Dict[str, Any]],
        all_parent_records: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        Disambiguates multiple candidates sharing the same name using category, item group,
        variant, code, or description attributes present in source row_data and candidate records.

        Returns:
            (filtered_candidates, match_method, explanation)
        """
        if not candidates or len(candidates) <= 1 or not row_data:
            return (candidates, None, None)

        src_info = cls.discover_source_classification_values(row_data)

        # 1. Unique Item Code / Business Identifier matching
        if src_info["codes"]:
            for _, code_val in src_info["codes"]:
                norm_c = normalize_cell_value(code_val)
                lower_c = code_val.lower().strip()
                code_matches = []
                for cand in candidates:
                    rec = cand.get("record", {})
                    rec_vals = cls.get_record_classification_values(rec, all_parent_records)
                    cand_codes = [normalize_cell_value(c) for c in rec_vals["code"]] + [c.lower() for c in rec_vals["code"]]
                    for k, v in rec.items():
                        if "code" in k.lower() or "sku" in k.lower() or "id" in k.lower():
                            cand_codes.append(str(v).strip().lower())
                            cand_codes.append(normalize_cell_value(str(v)))
                    if norm_c in cand_codes or lower_c in cand_codes:
                        code_matches.append(cand)
                if len(code_matches) == 1:
                    return (code_matches, "COMPOSITE_CODE_MATCH", f"Disambiguated candidate uniquely by item code '{code_val}'.")
                elif len(code_matches) > 1:
                    candidates = code_matches

        # 2. Category matching
        if src_info["categories"] and len(candidates) > 1:
            for cat_col, cat_val in src_info["categories"]:
                norm_cat = normalize_cell_value(cat_val)
                cat_tokens = set(tokenize_cell_value(cat_val))
                cat_matches = []
                for cand in candidates:
                    rec = cand.get("record", {})
                    rec_vals = cls.get_record_classification_values(rec, all_parent_records)
                    cand_cats = [normalize_cell_value(c) for c in rec_vals["category"]]
                    if norm_cat in cand_cats:
                        cat_matches.append(cand)
                    elif any(cat_tokens and set(tokenize_cell_value(c)).intersection(cat_tokens) for c in rec_vals["category"]):
                        cat_matches.append(cand)

                if len(cat_matches) == 1:
                    return (cat_matches, "COMPOSITE_CATEGORY_LOOKUP", f"Disambiguated candidate uniquely using category '{cat_val}' (from '{cat_col}').")
                elif len(cat_matches) > 1:
                    candidates = cat_matches

        # 3. Item Group matching
        if src_info["groups"] and len(candidates) > 1:
            for grp_col, grp_val in src_info["groups"]:
                norm_grp = normalize_cell_value(grp_val)
                grp_tokens = set(tokenize_cell_value(grp_val))
                grp_matches = []
                for cand in candidates:
                    rec = cand.get("record", {})
                    rec_vals = cls.get_record_classification_values(rec, all_parent_records)
                    cand_grps = [normalize_cell_value(g) for g in rec_vals["group"]]
                    if norm_grp in cand_grps:
                        grp_matches.append(cand)
                    elif any(grp_tokens and set(tokenize_cell_value(g)).intersection(grp_tokens) for g in rec_vals["group"]):
                        grp_matches.append(cand)

                if len(grp_matches) == 1:
                    return (grp_matches, "COMPOSITE_GROUP_LOOKUP", f"Disambiguated candidate uniquely using item group '{grp_val}' (from '{grp_col}').")
                elif len(grp_matches) > 1:
                    candidates = grp_matches

        # 4. Packaging Variant / Portion matching (e.g. '2pc', 'Parcel', 'Online', '500ml')
        if len(candidates) > 1:
            # Check variant column or full text in row
            all_text_tokens = set()
            for v_str in src_info["all_attributes"].values():
                all_text_tokens.update(tokenize_cell_value(str(v_str)))

            VARIANT_SIGNALS = ("2pc", "3pc", "4pc", "parcel", "online", "combo", "half", "full", "small", "medium", "large", "regular", "special")
            active_signals = {sig for sig in VARIANT_SIGNALS if sig in all_text_tokens}

            if active_signals:
                variant_matches = []
                for cand in candidates:
                    rec = cand.get("record", {})
                    cand_tokens = set()
                    for v in rec.values():
                        if v is not None:
                            cand_tokens.update(tokenize_cell_value(str(v)))
                    cand_name_tokens = set(tokenize_cell_value(str(cand.get("name", ""))))
                    all_cand_tokens = cand_tokens.union(cand_name_tokens)

                    # If all active signals are present in candidate
                    if active_signals.issubset(all_cand_tokens):
                        variant_matches.append(cand)

                if len(variant_matches) == 1:
                    return (variant_matches, "COMPOSITE_VARIANT_LOOKUP", f"Disambiguated candidate uniquely using variant attribute(s) {sorted(list(active_signals))}.")
                elif len(variant_matches) > 1:
                    candidates = variant_matches

        return (candidates, None, None)

    @staticmethod
    def identify_parent_name_column(parent_table_meta: TableMetadata) -> Optional[str]:
        """
        Dynamically discovers the best identifying business-name column in a parent table.
        Scores candidate columns by entity name, text type, unique constraints, and naming patterns.
        Strictly rejects date/time, numeric, and identifier columns.
        """
        best_col: Optional[str] = None
        best_score = 0

        t_name_lower = parent_table_meta.name.lower()
        t_norm = normalize_column_name(parent_table_meta.name)
        core_tokens = [stem_token(t) for t in t_norm.split() if t not in ("master", "table", "tbl", "info", "dim", "mst", "data")]
        t_root = core_tokens[0] if core_tokens else t_name_lower.rstrip('s')

        for col in parent_table_meta.columns:
            if col.primary_key or getattr(col, "identity", False):
                continue

            c_name = col.name
            c_lower = c_name.lower()
            c_norm = normalize_column_name(c_name)
            col_type = col.type.upper()
            is_text = any(t in col_type for t in ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "STRING"))

            if not is_text:
                continue

            # Skip date/time, timestamp, and audit columns
            if any(k in c_lower for k in ("date", "time", "year", "created", "modified", "timestamp", "stamp", "guid")):
                continue

            # Skip ID columns that end with _id or id
            if c_lower.endswith("_id") or (c_lower.endswith("id") and c_lower != "taxid"):
                continue

            # Penalize group, category, style, mis, type, sort, classification columns
            is_classification = any(k in c_lower for k in ("group", "category", "section", "style", "mis", "type", "class", "sort", "status"))

            score = 0

            # 1. Direct entity root column (e.g. Item in ItemsMaster, Product in ProductsMaster, Category in Categories)
            if c_lower in (t_root, f"{t_root}s", t_norm) or c_norm in (t_root, f"{t_root}s"):
                score += 115
            # 2. Exact entity name column (e.g. Outlet_Name in Outlets, Customer_Name in Customer, Item_Name in ItemsMaster)
            elif c_lower in (f"{t_name_lower}_name", f"{t_name_lower}name", f"{t_name_lower.rstrip('s')}_name", f"{t_name_lower.rstrip('s')}name", f"{t_root}_name", f"{t_root}name"):
                score += 100
            elif "name" in c_norm and any(token in core_tokens for token in c_norm.split()):
                score += 95
            elif "name" in c_norm and any(token in t_norm for token in c_norm.split()):
                score += 90
            elif c_lower in ("name", "title", "business_name", "display_name"):
                score += 85
            elif c_lower == "description" or c_lower == f"{t_root}_description":
                score += 75
            elif "name" in c_lower:
                score += 70

            if is_classification:
                score -= 40

            if col.unique:
                score += 15

            # Require a solid confidence score to be treated as an entity business name
            if score >= 70 and score > best_score:
                best_score = score
                best_col = c_name

        return best_col

    @staticmethod
    def find_matching_excel_column(
        df_columns: List[str],
        parent_table_name: str,
        parent_name_column: Optional[str],
        column_mappings: List[ColumnMappingResult],
        child_column_name: Optional[str] = None
    ) -> Optional[str]:
        """
        Finds the corresponding business-name column available in the uploaded Excel file.
        Checks:
        0. Column mapping or direct/synonym match targeting child_column_name (e.g. assign_to for Assigned_Outlet_ID).
        1. Directly confirmed column mapping targeting parent_table.parent_name_column.
        2. Exact or normalized column name match with parent_name_column.
        3. Synonym dictionary matching for parent entity name.
        """
        # Exclude numeric and date/time source columns from being picked as entity names
        NON_NAME_KEYWORDS = ("date", "time", "qty", "quantity", "amount", "total", "tax", "price", "discount", "cost", "rate", "bill_no", "billno")

        def is_clean_name_col(c: str) -> bool:
            cl = c.lower().strip()
            return not any(k in cl for k in NON_NAME_KEYWORDS)

        p_norm = normalize_column_name(parent_table_name)
        p_core_tokens = [stem_token(t) for t in p_norm.split() if t not in ("master", "table", "tbl", "info", "dim", "mst", "data")]
        p_root = p_core_tokens[0] if p_core_tokens else parent_table_name.rstrip('s').lower()

        # 0a. Check if child table has a text column mapped from an Excel column (e.g. OrderLineItems.Description mapped from item_name)
        # For item lookups, child table Description/Item is the primary source of the item name
        if child_column_name:
            c_prefix = child_column_name.lower().replace("_id", "").replace("id", "")
            for m in column_mappings:
                if (
                    m.database_column.lower() in ("description", "desc", "item", "item_name", "name", "title")
                    and m.excel_column in df_columns
                    and is_clean_name_col(m.excel_column)
                    and (not c_prefix or c_prefix in m.database_table.lower() or c_prefix in m.database_column.lower() or p_root in m.database_table.lower() or p_root in m.database_column.lower())
                ):
                    return m.excel_column

        # 0b. Check child_column_name specific source column if specified
        if child_column_name:
            c_clean = child_column_name.lower().strip()
            c_norm = normalize_column_name(child_column_name)
            # Direct mapping targeting this specific child column
            for m in column_mappings:
                if (
                    m.database_column.lower() == c_clean
                    and m.excel_column in df_columns
                    and is_clean_name_col(m.excel_column)
                ):
                    return m.excel_column

            # Check if an Excel column matches child_column_name or its prefix
            for col in df_columns:
                if not is_clean_name_col(col):
                    continue
                col_norm = normalize_column_name(col)
                if col_norm == c_norm or col.lower().strip() == c_clean:
                    return col
                if "assign" in c_norm and any(a in col_norm for a in ("assign", "assigned")):
                    return col

        if not parent_name_column:
            return None

        # 1. Check if any Excel column is mapped to parent_table.parent_name_column
        for m in column_mappings:
            if (
                m.database_table.lower() == parent_table_name.lower()
                and m.database_column.lower() == parent_name_column.lower()
                and m.excel_column in df_columns
                and is_clean_name_col(m.excel_column)
            ):
                return m.excel_column

        # 2. Check exact and normalized match across Excel columns
        candidates_to_match = [
            normalize_column_name(parent_name_column),
            parent_name_column.lower(),
            f"{p_root} name",
            f"{p_root}_name",
            f"{normalize_column_name(parent_name_column)} name",
            f"{parent_table_name.rstrip('s').lower()} name",
            f"{parent_table_name.rstrip('s').lower()}_name",
            f"{p_norm} name",
        ]
        if is_item_master_target(parent_table_name, "", child_column_name or ""):
            candidates_to_match.extend(["description", "item description", "item name", "item"])

        for col in df_columns:
            if not is_clean_name_col(col):
                continue
            norm_col = normalize_column_name(col)
            clean_col = col.lower().strip()
            if any(cand == norm_col or cand == clean_col for cand in candidates_to_match):
                return col

        # 3. Synonym matching: Check registered business concepts (TEXT only)
        p_concept_match = (
            synonym_registry.find_concept_for_term(parent_name_column)
            or synonym_registry.find_concept_for_term(f"{parent_table_name} name")
            or synonym_registry.find_concept_for_term(parent_table_name)
        )
        if p_concept_match:
            p_concept = p_concept_match[1]
            for col in df_columns:
                if not is_clean_name_col(col):
                    continue
                col_concept_match = synonym_registry.find_concept_for_term(col)
                if col_concept_match and col_concept_match[1].id == p_concept.id:
                    return col

        p_lower = parent_table_name.lower().rstrip('s')
        for col in df_columns:
            if not is_clean_name_col(col):
                continue
            norm_col = normalize_column_name(col)
            for concept in synonym_registry.get_concepts():
                if getattr(concept, "expected_type", "TEXT") not in ("TEXT", "STRING"):
                    continue
                norm_syns = [normalize_column_name(s) for s in concept.synonyms]
                covers_parent = (
                    (parent_name_column and normalize_column_name(parent_name_column) in norm_syns)
                    or normalize_column_name(parent_table_name) in norm_syns
                    or f"{normalize_column_name(parent_table_name)} name" in norm_syns
                    or p_lower in concept.id.lower()
                    or p_lower in concept.name.lower()
                )
                if covers_parent and norm_col in norm_syns:
                    return col

        # 4. Domain & Entity keyword fallback (e.g. Outlets -> restaurant_name, store_name, branch_name)
        OUTLET_ENTITY_TOKENS = ("restaurant", "outlet", "branch", "store", "pos", "location")
        if p_lower in ("outlet", "store", "branch", "restaurant", "pos"):
            for col in df_columns:
                if not is_clean_name_col(col):
                    continue
                norm_col = normalize_column_name(col)
                if any(t in norm_col for t in OUTLET_ENTITY_TOKENS) and "name" in norm_col:
                    return col

        # 5. General fallback: contains parent entity token and 'name'
        for col in df_columns:
            if not is_clean_name_col(col):
                continue
            norm_col = normalize_column_name(col)
            if p_lower in norm_col and "name" in norm_col:
                return col

        return None

    @classmethod
    def discover_relationships(
        cls,
        schema: DatabaseSchemaMetadata,
        df_columns: List[str],
        column_mappings: List[ColumnMappingResult],
        active_tables: List[str]
    ) -> List[LookupRelationship]:
        """
        Inspects the actual SQL Server schema metadata to dynamically identify foreign-key relationships.
        Derives relationships without any hardcoded table or column names.
        """
        schema_tables = {t.name: t for t in schema.tables}
        relationships: List[LookupRelationship] = []
        seen_keys: Set[Tuple[str, str]] = set()

        for child_tbl_name in active_tables:
            if child_tbl_name not in schema_tables:
                continue
            child_meta = schema_tables[child_tbl_name]
            col_meta_map = {c.name: c for c in child_meta.columns}

            # Group column mappings by database column for this child table
            db_to_excel = {
                m.database_column: m.excel_column
                for m in column_mappings
                if m.database_table == child_tbl_name and m.excel_column in df_columns
            }

            # 1. Explicit foreign keys from SQL Server catalog metadata
            for fk in child_meta.foreign_keys:
                parent_tbl_name = fk.target_table
                if parent_tbl_name not in schema_tables:
                    continue

                parent_meta = schema_tables[parent_tbl_name]
                child_col = fk.constrained_column
                parent_id_col = fk.target_column

                c_meta = col_meta_map.get(child_col)
                is_req = bool(c_meta and not c_meta.nullable and c_meta.default is None and not getattr(c_meta, "identity", False))

                parent_name_col = cls.identify_parent_name_column(parent_meta)
                excel_name_col = cls.find_matching_excel_column(
                    df_columns=df_columns,
                    parent_table_name=parent_tbl_name,
                    parent_name_column=parent_name_col,
                    column_mappings=column_mappings,
                    child_column_name=child_col
                )
                excel_id_col = db_to_excel.get(child_col)

                rel = LookupRelationship(
                    child_table=child_tbl_name,
                    child_column=child_col,
                    parent_table=parent_tbl_name,
                    parent_id_column=parent_id_col,
                    parent_name_column=parent_name_col,
                    excel_name_column=excel_name_col,
                    excel_id_column=excel_id_col,
                    is_required=is_req
                )
                relationships.append(rel)
                seen_keys.add((child_tbl_name, child_col))

            # 2. Inferred foreign keys (columns ending with _id or id matching known parent tables)
            for c_name, c_meta in col_meta_map.items():
                if (child_tbl_name, c_name) in seen_keys:
                    continue
                if getattr(c_meta, "identity", False) or c_meta.primary_key:
                    continue

                c_norm = normalize_column_name(c_name)
                c_lower = c_name.lower()

                if c_lower.endswith("_id") or c_lower.endswith("id"):
                    # Find candidate parent table in schema
                    c_tokens = [stem_token(t) for t in c_norm.split() if t not in ("id", "code", "key", "num", "no")]
                    # Target entity root is the primary entity token (e.g. 'outlet' in 'order outlet id', 'item' in 'item id')
                    c_entity = c_tokens[-1] if c_tokens else c_lower.replace("_id", "").replace("id", "")

                    best_cand = None
                    best_cand_score = 0
                    child_norm = child_tbl_name.lower().replace("dbo.", "").strip()
                    for candidate_tbl_name, cand_meta in schema_tables.items():
                        if candidate_tbl_name == child_tbl_name:
                            continue
                        cand_norm_t = candidate_tbl_name.lower().replace("dbo.", "").strip()
                        # A header table (like Order) must never infer downstream child tables (OrderLineItems) as its parent
                        if child_norm in ("order", "orders") and any(k in cand_norm_t for k in ("lineitem", "detail", "modifier")):
                            continue
                        cand_lower = candidate_tbl_name.lower()
                        cand_norm = normalize_column_name(candidate_tbl_name)
                        cand_tokens = [stem_token(t) for t in cand_norm.split() if t not in ("master", "table", "tbl", "info", "dim", "mst", "data")]
                        cand_entity = cand_tokens[0] if cand_tokens else cand_lower.rstrip("s")

                        cand_pks = [p.name.lower() for p in cand_meta.columns if p.primary_key or getattr(p, "identity", False)]
                        has_pk_match = any(c_lower == pk or (c_tokens and c_tokens[-1] in pk) or pk in c_lower for pk in cand_pks)

                        score = 0
                        # 1. Entity and name matching
                        if cand_lower in c_lower or cand_lower.rstrip("s") in c_lower:
                            score = 100
                        elif c_tokens and cand_tokens == c_tokens:
                            score = 95
                        elif c_entity and (c_entity == cand_entity or c_entity in cand_tokens or cand_entity in c_tokens):
                            score = 90
                        elif c_entity and (c_entity in cand_lower or cand_entity in c_lower):
                            score = 80

                        # Check if primary key of candidate table matches
                        if has_pk_match:
                            score += 30
                        elif any(c.name.lower() == c_lower for c in cand_meta.columns):
                            score += 10

                        # Master / dimension table indicators (e.g. ItemsMaster vs ItemModifiers)
                        if any(k in cand_lower for k in ("master", "mst", "dim")):
                            score += 20

                        # Penalize secondary, discard, archive, backup, sync, log, modifier tables
                        if any(k in cand_lower for k in ("discard", "history", "archive", "sync", "backup", "temp", "log", "audit", "reinstate")):
                            score -= 60
                        if any(k in cand_lower for k in ("modifier", "detail", "itemcombodefinations", "item_combo")):
                            score -= 25

                        if score > best_cand_score and score >= 80:
                            best_cand_score = score
                            best_cand = (candidate_tbl_name, cand_meta, has_pk_match or any(c.name.lower() == c_lower for c in cand_meta.columns))

                    if best_cand:
                        candidate_tbl_name, cand_meta, has_matching_col = best_cand
                        matching_cols = [c.name for c in cand_meta.columns if c.name.lower() == c_lower]
                        pk_cols = [p.name for p in cand_meta.columns if p.primary_key]
                        identity_cols = [p.name for p in cand_meta.columns if getattr(p, "identity", False)]

                        # If candidate table has an exact column name match, use it (e.g. ItemsMaster.Item_ID)
                        if matching_cols:
                            parent_id_col = matching_cols[0]
                        elif pk_cols:
                            parent_id_col = pk_cols[0]
                        elif identity_cols:
                            parent_id_col = identity_cols[0]
                        else:
                            parent_id_col = c_name  # fallback

                        is_req = bool(not c_meta.nullable and c_meta.default is None)
                        parent_name_col = cls.identify_parent_name_column(cand_meta)
                        excel_name_col = cls.find_matching_excel_column(
                            df_columns=df_columns,
                            parent_table_name=candidate_tbl_name,
                            parent_name_column=parent_name_col,
                            column_mappings=column_mappings,
                            child_column_name=c_name
                        )
                        excel_id_col = db_to_excel.get(c_name)

                        rel = LookupRelationship(
                            child_table=child_tbl_name,
                            child_column=c_name,
                            parent_table=candidate_tbl_name,
                            parent_id_column=parent_id_col,
                            parent_name_column=parent_name_col,
                            excel_name_column=excel_name_col,
                            excel_id_column=excel_id_col,
                            is_required=is_req
                        )
                        relationships.append(rel)
                        seen_keys.add((child_tbl_name, c_name))

        return relationships

    @classmethod
    def load_parent_records(
        cls,
        engine: Engine,
        schema: DatabaseSchemaMetadata,
        relationships: List[LookupRelationship]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Queries SQL Server using parameterized / metadata-safe SELECT queries to load existing
        records from referenced parent tables for dynamic name-to-ID matching.
        """
        dialect_name = engine.name
        schema_tables = {t.name: t for t in schema.tables}
        records_by_table: Dict[str, List[Dict[str, Any]]] = {}

        # Collect unique parent tables to query strictly from discovered relationships,
        # strictly excluding entity target tables being migrated from Excel so no DB duplicate check queries occur.
        target_entity_names = {
            "customer", "bills", "bill", "order", "orders", "orderlineitems", "settlement", "settlements"
        }
        tables_to_query: Set[str] = {
            rel.parent_table for rel in relationships
            if rel.parent_table in schema_tables
            and rel.parent_table.lower().replace("dbo.", "").strip() not in target_entity_names
        }

        with engine.connect() as conn:
            for table_name in tables_to_query:
                if table_name not in schema_tables:
                    continue

                t_meta = schema_tables[table_name]
                # Fetch only columns needed for key/FK resolution. Selecting every column
                # (including wide text fields) from up to 50k rows per lookup table made
                # preview exceed the frontend's 120-second request timeout.
                pk_cols = [c.name for c in t_meta.columns if c.primary_key]
                ident_cols = [c.name for c in t_meta.columns if any(k in c.name.lower() for k in ("name", "code", "sku", "item", "address", "short_code", "phone", "contact", "title", "group", "category"))]
                rel_cols = [rel.parent_id_column for rel in relationships if rel.parent_table == table_name and rel.parent_id_column] + \
                           [rel.parent_name_column for rel in relationships if rel.parent_table == table_name and rel.parent_name_column]
                needed_set = set(pk_cols + ident_cols + rel_cols)

                safe_cols = [
                    c.name for c in t_meta.columns
                    if (not needed_set or c.name in needed_set)
                    and not any(b in c.type.upper() for b in ("BINARY", "IMAGE", "VARBINARY", "GEOGRAPHY", "GEOMETRY"))
                ]
                if not safe_cols:
                    safe_cols = [c.name for c in t_meta.columns[:8]]

                quoted_tbl = quote_identifier(table_name, dialect_name)
                quoted_cols = [quote_identifier(c, dialect_name) for c in safe_cols]

                try:
                    if dialect_name == "mssql":
                        query_str = f"SELECT TOP 10000 {', '.join(quoted_cols)} FROM {quoted_tbl}"
                    else:
                        query_str = f"SELECT {', '.join(quoted_cols)} FROM {quoted_tbl} LIMIT 10000"

                    res = conn.execute(text(query_str)).mappings().all()
                    record_list = [dict(r) for r in res]
                    records_by_table[table_name] = record_list
                    logger.debug(f"[LOOKUP_RESOLVER] Loaded {len(record_list)} records from {table_name}")
                except Exception as query_err:
                    logger.warning(f"[LOOKUP_RESOLVER] Could not query {table_name}: {query_err}")
                    records_by_table[table_name] = []

        return records_by_table

    @staticmethod
    def _item_base_and_variant(value: Any) -> Tuple[str, Set[str]]:
        """Return a conservative canonical item base and explicit size/channel signals.

        Channel suffixes (Online/Parcel) are tracked separately from serving-size
        variants. They may be ignored only as a fallback when an exact channel item
        does not exist; 1pc/2pc/3pc/4pc are never interchangeable.
        """
        raw = str(value or "").strip()
        norm = normalize_cell_value(raw)
        tokens = norm.split()
        aliases = {
            "1pc": {"1pc", "1pcs", "1 piece", "1 pieces", "single"},
            "2pc": {"2pc", "2pcs", "2 piece", "2 pieces", "double"},
            "3pc": {"3pc", "3pcs", "3 piece", "3 pieces", "triple"},
            "4pc": {"4pc", "4pcs", "4 piece", "4 pieces"},
            "parcel": {"parcel", "takeaway", "take away"},
            "online": {"online"},
            "half": {"half"},
            "full": {"full"},
            "small": {"small"},
            "medium": {"medium"},
            "large": {"large"},
            "250ml": {"250ml", "250 ml"},
            "500ml": {"500ml", "500 ml"},
            "250gm": {"250gm", "250 g", "250 gm", "250 gram", "250 grams", "250g"},
            "500gm": {"500gm", "500 g", "500 gm", "500 gram", "500 grams", "500g"},
        }
        alias_map = {alias: canonical for canonical, words in aliases.items() for alias in words}
        variant_hits: Set[str] = set()
        remaining = list(tokens)
        for phrase, canonical in sorted(alias_map.items(), key=lambda x: len(x[0].split()), reverse=True):
            phrase_tokens = phrase.split()
            for i in range(len(remaining) - len(phrase_tokens) + 1):
                if remaining[i:i + len(phrase_tokens)] == phrase_tokens:
                    variant_hits.add(canonical)
                    remaining[i:i + len(phrase_tokens)] = ["__variant__"] * len(phrase_tokens)
                    break

        # Canonicalize only known spelling/spacing variants; preserve meaningful
        # words such as ingredients, serving sizes and product types.
        base_tokens = [t for t in remaining if t != "__variant__"]
        spelling_aliases = {
            "idly": "idli",
            "bhath": "bath",
            "bhat": "bath",
            "currry": "curry",
            "obbattu": "holige",
            "holige": "holige",
            "szechuan": "schezwan",
            "schezwan": "schezwan",
        }
        base_tokens = [spelling_aliases.get(t, t) for t in base_tokens]
        base = " ".join(base_tokens).strip()
        # Common compound spelling variation: "bisibele" vs "bisi bele".
        base = base.replace("bisibele", "bisi bele")
        return base, variant_hits

    @classmethod
    def _variant_alias_candidates(
        cls,
        source_name: str,
        db_records: List[Dict[str, Any]],
        parent_id_column: str,
        parent_name_column: str,
        row_data: Optional[Dict[str, Any]],
        all_parent_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve explicit item variants with category/group as ranking signals.

        Exact name lookup runs first. Here, Online/Parcel are treated as channel
        suffixes only for fallback when no exact name exists. Portion variants remain
        strict (e.g. 1pc must never match 2pc). If classification labels differ across
        the source and master taxonomy, a single otherwise-unique semantic candidate
        may still match; multiple candidates require classification evidence.
        """
        source_base, source_variants = cls._item_base_and_variant(source_name)
        if not source_base:
            return []

        source_info = cls.discover_source_classification_values(row_data or {})
        source_categories = [normalize_cell_value(v) for _, v in source_info["categories"]]
        source_groups = [normalize_cell_value(v) for _, v in source_info["groups"]]
        channel_variants = {"online", "parcel"}
        source_portions = source_variants - channel_variants
        source_channels = source_variants & channel_variants

        candidates: List[Dict[str, Any]] = []
        for record in db_records:
            candidate_name = record.get(parent_name_column)
            candidate_base, candidate_variants = cls._item_base_and_variant(candidate_name)
            if not candidate_base or candidate_base != source_base:
                continue

            candidate_portions = candidate_variants - channel_variants
            candidate_channels = candidate_variants & channel_variants
            # Portion and fulfillment channel are meaningful item variants. Require
            # channel equality (Online != Parcel != neutral) and never silently map
            # a Parcel SKU to a dine-in/base SKU.
            if source_channels != candidate_channels:
                continue
            if source_portions and candidate_portions and not source_portions.intersection(candidate_portions):
                continue
            if source_portions and not candidate_portions:
                continue
            if not source_portions and candidate_portions:
                continue

            item_id = record.get(parent_id_column)
            if item_id is None:
                continue
            candidates.append({
                "id": item_id,
                "name": str(candidate_name or item_id),
                "record": record,
                "_classification": cls.get_record_classification_values(record, all_parent_records),
            })

        if not candidates:
            return []

        # Use source unit price as a validation/disambiguation signal when the
        # Excel row provides it and the master record has a price column. Price
        # is never used alone to identify an item. If prices are available but
        # none of the semantic candidates agrees, refuse the alias match.
        source_price = None
        if isinstance(row_data, dict):
            for key, value in row_data.items():
                key_norm = normalize_column_name(str(key))
                if key_norm in ("item price", "price", "unit price", "selling price", "menu price") and not is_empty_or_na(value):
                    try:
                        source_price = float(str(value).replace(",", "").replace("₹", "").strip())
                        break
                    except (TypeError, ValueError):
                        continue

        if source_price is not None:
            priced_candidates = []
            candidates_with_price = 0
            for candidate in candidates:
                record = candidate.get("record", {})
                master_price = None
                for key, value in record.items():
                    key_norm = normalize_column_name(str(key))
                    if key_norm in ("item price", "price", "unit price", "selling price", "menu price") and not is_empty_or_na(value):
                        try:
                            master_price = float(str(value).replace(",", "").replace("₹", "").strip())
                            break
                        except (TypeError, ValueError):
                            continue
                if master_price is None:
                    continue
                candidates_with_price += 1
                if math.isclose(source_price, master_price, rel_tol=0.0, abs_tol=0.01):
                    priced_candidates.append(candidate)
            if candidates_with_price and priced_candidates:
                # Price is a tie-breaker only. Historical OrderLineItems.Item_Price may
                # be a transaction/extended/tax-inclusive price and can legitimately
                # differ from ItemsMaster.Item_Price. Never reject a unique name/variant
                # candidate solely because those two price fields differ.
                candidates = priced_candidates

        def compatible(values: List[str], source_values: List[str]) -> bool:
            normalized = [normalize_cell_value(v) for v in values if normalize_cell_value(v)]
            return bool(normalized and source_values and any(
                sv == dv or sv in dv or dv in sv
                for sv in source_values for dv in normalized
            ))

        # Category and group are strong disambiguators if they align, but not a
        # mandatory gate because source categories (e.g. South Indian) may differ
        # from master menu groups (e.g. Thindi) for the same item.
        ranked = []
        source_channels = source_variants & channel_variants
        for cand in candidates:
            cls_values = cand.pop("_classification")
            cat_ok = compatible(cls_values["category"], source_categories)
            group_ok = compatible(cls_values["group"], source_groups)
            _, candidate_variants = cls._item_base_and_variant(cand["name"])
            candidate_channels = candidate_variants & channel_variants
            # Prefer same channel if present; otherwise prefer a neutral base item
            # over a different channel-specific item (Online should not fall back
            # to a Parcel-only SKU when a neutral item exists).
            channel_score = 2 if source_channels and candidate_channels == source_channels else (
                1 if not candidate_channels else 0
            )
            ranked.append((int(cat_ok) + int(group_ok), channel_score, cand))
        best_score = max((score, channel_score) for score, channel_score, _ in ranked)
        best = [cand for score, channel_score, cand in ranked if (score, channel_score) == best_score]

        # A unique semantic candidate is safe even if category taxonomy differs.
        # If duplicates remain, refuse to guess.
        unique: Dict[str, Dict[str, Any]] = {}
        for cand in best:
            unique[str(cand["id"]).strip()] = cand
        return list(unique.values())

    @classmethod
    def match_name_to_id(
        cls,
        excel_name: Any,
        parent_table: str,
        parent_id_column: str,
        parent_name_column: str,
        db_records: List[Dict[str, Any]],
        user_override_id: Optional[Any] = None,
        index: Optional[TableLookupIndex] = None,
        row_data: Optional[Dict[str, Any]] = None,
        all_parent_records: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ) -> Tuple[Optional[Any], Optional[str], str, str, float, List[Dict[str, Any]], str]:
        """
        Matches a single Excel business name against actual database records from SQL Server.
        Follows strict safety rules:
        - 1 reliable match: return (resolved_id, matched_db_name, "MATCHED", "DATABASE_LOOKUP", 1.0, candidates, msg)
        - 0 matches: return (None, None, "UNRESOLVED", "UNRESOLVED", 0.0, [], msg)
        - > 1 matches: attempts composite disambiguation (category/group/code/variant); if still ambiguous, return AMBIGUOUS
        - Missing name: return (None, None, "MISSING_NAME", "NULL", 0.0, [], msg)
        - Never chooses the first record arbitrarily.
        """
        # Missing name check
        if excel_name is None:
            return (None, None, "MISSING_NAME", "NULL", 0.0, [], "Excel identifying name is missing/empty.")

        str_name = str(excel_name).strip()
        if not str_name or str_name.lower() in ("nan", "none", "null", "nat"):
            return (None, None, "MISSING_NAME", "NULL", 0.0, [], "Excel identifying name is missing/empty.")

        if not db_records:
            return (
                None,
                None,
                "UNRESOLVED",
                "UNRESOLVED",
                0.0,
                [],
                f"Referenced database table '{parent_table}' has no existing records."
            )

        if index is None:
            index = cls.get_or_create_index(db_records, parent_id_column, parent_name_column)

        # 0. Specialized Outlet matching for Outlets table
        if parent_table.lower() in ("outlets", "dbo.outlets"):
            out_res = OutletMatcher.match_outlet_name(
                source_name=str_name,
                db_records=db_records,
                id_col=parent_id_column,
                name_col=parent_name_column
            )
            out_status = out_res.get("status")
            if out_status == "MATCHED":
                return (
                    out_res["outlet_id"],
                    out_res.get("matched_outlet_name"),
                    "MATCHED",
                    "DATABASE_LOOKUP",
                    out_res.get("confidence", 0.95),
                    out_res.get("candidates", []),
                    out_res.get("message", f"Resolved outlet ID '{out_res['outlet_id']}'.")
                )
            elif out_status == "AMBIGUOUS":
                return (
                    None,
                    None,
                    "AMBIGUOUS",
                    "AMBIGUOUS",
                    0.5,
                    out_res.get("candidates", []),
                    out_res.get("message", f"Ambiguous outlet match for '{str_name}'. User review required.")
                )
            else:
                return (
                    None,
                    None,
                    "UNRESOLVED",
                    "UNRESOLVED",
                    0.0,
                    [],
                    f"Referenced name '{str_name}' not found in database table '{parent_table}'."
                )

        # 0. User override takes top priority
        if user_override_id is not None:
            override_str = str(user_override_id).strip()
            matched_rec = index.override_map.get(override_str)
            matched_name = matched_rec["name"] if matched_rec else None
            return (
                user_override_id,
                matched_name,
                "MATCHED",
                "USER_OVERRIDE",
                1.0,
                [],
                f"Resolved via user override to ID '{user_override_id}'."
            )

        # 1. Exact matches evaluated via O(1) hash map
        exact_matches = index.exact_map.get(str_name, [])
        if len(exact_matches) == 1:
            rec = exact_matches[0]
            return (
                rec["id"],
                rec["name"],
                "MATCHED",
                "DATABASE_LOOKUP",
                1.0,
                exact_matches,
                f"Exact match on '{rec['name']}' in database table '{parent_table}'."
            )
        elif len(exact_matches) > 1:
            # Check if all exact matches point to the same database ID
            seen_cand_ids = {m["id"] for m in exact_matches}
            if len(seen_cand_ids) == 1:
                rec = exact_matches[0]
                return (
                    rec["id"],
                    rec["name"],
                    "MATCHED",
                    "DATABASE_LOOKUP",
                    1.0,
                    exact_matches,
                    f"Exact match on '{rec['name']}' in database table '{parent_table}'."
                )

            # Multiple distinct candidates: try composite disambiguation if row_data available
            if row_data:
                filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                    candidates=exact_matches,
                    row_data=row_data,
                    all_parent_records=all_parent_records
                )
                if len(filt_cands) == 1:
                    rec = filt_cands[0]
                    return (
                        rec["id"],
                        rec["name"],
                        "MATCHED",
                        m_method or "COMPOSITE_LOOKUP",
                        0.98,
                        filt_cands,
                        m_expl or f"Disambiguated '{str_name}' in database table '{parent_table}'."
                    )
                elif len(filt_cands) > 1:
                    exact_matches = filt_cands

            return (
                None,
                None,
                "AMBIGUOUS",
                "AMBIGUOUS",
                0.5,
                exact_matches,
                f"Ambiguous: Name '{str_name}' matched {len(exact_matches)} database records in '{parent_table}'. User review required."
            )

        # 2. Normalized matches evaluated via O(1) hash map
        norm_excel = normalize_cell_value(str_name)
        lower_excel = str_name.lower()
        norm_raw = index.norm_map.get(norm_excel) or index.norm_map.get(lower_excel, [])

        # Deduplicate matches by ID
        seen_cand_ids = set()
        normalized_matches = []
        for c in norm_raw:
            if c["id"] not in seen_cand_ids:
                seen_cand_ids.add(c["id"])
                normalized_matches.append(c)

        if len(normalized_matches) == 1:
            rec = normalized_matches[0]
            return (
                rec["id"],
                rec["name"],
                "MATCHED",
                "DATABASE_LOOKUP",
                0.95,
                normalized_matches,
                f"Normalized match ('{str_name}' -> '{rec['name']}') in database table '{parent_table}'."
            )
        elif len(normalized_matches) > 1:
            if row_data:
                filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                    candidates=normalized_matches,
                    row_data=row_data,
                    all_parent_records=all_parent_records
                )
                if len(filt_cands) == 1:
                    rec = filt_cands[0]
                    return (
                        rec["id"],
                        rec["name"],
                        "MATCHED",
                        m_method or "COMPOSITE_LOOKUP",
                        0.95,
                        filt_cands,
                        m_expl or f"Disambiguated '{str_name}' in database table '{parent_table}'."
                    )
                elif len(filt_cands) > 1:
                    normalized_matches = filt_cands

            return (
                None,
                None,
                "AMBIGUOUS",
                "AMBIGUOUS",
                0.5,
                normalized_matches,
                f"Ambiguous: Normalized name '{str_name}' matched {len(normalized_matches)} database records in '{parent_table}'."
            )

        # 3. Strong semantic variant alias match (e.g. Idli (2pc) <-> Idli (double)).
        # Requires same base item name, explicit variant equivalence, and compatible category/group.
        variant_matches = cls._variant_alias_candidates(
            source_name=str_name,
            db_records=db_records,
            parent_id_column=parent_id_column,
            parent_name_column=parent_name_column,
            row_data=row_data,
            all_parent_records=all_parent_records,
        )
        if len(variant_matches) == 1:
            candidate = variant_matches[0]
            return (
                candidate["id"], candidate["name"], "MATCHED", "SEMANTIC_VARIANT_ALIAS",
                0.92, variant_matches,
                f"Resolved '{str_name}' to '{candidate['name']}' using an explicit portion-variant alias, matching base name, and compatible classification."
            )
        elif len(variant_matches) > 1:
            return (
                None, None, "AMBIGUOUS", "AMBIGUOUS_VARIANT_ALIAS", 0.5, variant_matches[:10],
                f"Multiple database items share the base name/variant alias for '{str_name}'. Category/group data is insufficient to select safely."
            )

        # Fulfilment channels are meaningful SKUs. If exact/normalized/semantic
        # matching did not resolve an Online or Parcel item, do not let token-fuzzy
        # matching silently map it to a neutral or different-channel item.
        _, unresolved_variants = cls._item_base_and_variant(str_name)
        if unresolved_variants & {"online", "parcel"}:
            return (
                None, None, "UNRESOLVED", "CHANNEL_VARIANT_NOT_FOUND", 0.0, [],
                f"No exact ItemsMaster SKU found for '{str_name}'. Online/Parcel items are not auto-mapped to a different channel or base SKU."
            )

        # 3. Fuzzy token matches evaluated (suggestions only - require confirmation)
        fuzzy_matches = []
        fuzzy_ids = set()
        for tok in norm_excel.split():
            if len(tok) >= 3 and tok in index.token_map:
                for c in index.token_map[tok]:
                    if c["id"] not in fuzzy_ids:
                        fuzzy_ids.add(c["id"])
                        fuzzy_matches.append(c)
                        if len(fuzzy_matches) >= 5:
                            break
            if len(fuzzy_matches) >= 5:
                break

        if fuzzy_matches:
            if row_data and len(fuzzy_matches) > 1:
                filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                    candidates=fuzzy_matches,
                    row_data=row_data,
                    all_parent_records=all_parent_records
                )
                if len(filt_cands) == 1:
                    rec = filt_cands[0]
                    return (
                        rec["id"],
                        rec["name"],
                        "MATCHED",
                        m_method or "COMPOSITE_LOOKUP",
                        0.90,
                        filt_cands,
                        m_expl or f"Disambiguated '{str_name}' in database table '{parent_table}'."
                    )
                elif len(filt_cands) > 1:
                    fuzzy_matches = filt_cands

            return (
                None,
                None,
                "AMBIGUOUS",
                "AMBIGUOUS",
                0.6,
                fuzzy_matches[:5],
                f"Uncertain: Name '{str_name}' partially matched {len(fuzzy_matches)} candidate(s) in '{parent_table}'. Confirmation required."
            )

        # 4. No matches
        return (
            None,
            None,
            "UNRESOLVED",
            "UNRESOLVED",
            0.0,
            [],
            f"Referenced name '{str_name}' not found in database table '{parent_table}'."
        )

    @classmethod
    def resolve_foreign_key_record(
        cls,
        child_table: str,
        child_column: str,
        parent_table: str,
        parent_id_column: str,
        parent_name_column: Optional[str] = None,
        db_records: Optional[List[Dict[str, Any]]] = None,
        excel_source_value: Any = None,
        row_resolved_entities: Optional[Dict[str, ResolvedEntityRecord]] = None,
        row_value_mappings: Optional[List[ValueMappingItem]] = None,
        row_data: Optional[Dict[str, Any]] = None,
        user_override_id: Optional[Any] = None,
        index: Optional[TableLookupIndex] = None,
        migration_cache: Optional[Dict[Any, ResolvedEntityRecord]] = None,
        all_parent_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        upload_id: Optional[str] = None,
        source_row_num: Optional[int] = None
    ) -> ResolvedEntityRecord:
        """
        Dynamically resolves a foreign key to a ResolvedEntityRecord with complete record retention:
        1. User override check (priority 1.0)
        2. Same-row entity reuse check (reuses previously resolved parent entity for same row if source is empty or matching)
        3. Migration-level cache lookup (O(1))
        4. Direct database match against parent_records (via TableLookupIndex)
        5. Value mapping inspection from row_value_mappings (direct match on parent table or correlated table)
        6. Correlated entity inspection from row_data and other tables (e.g. Customer.Location -> Outlet_ID)
        7. Ambiguity detection (if multiple distinct records match -> AMBIGUOUS)
        8. Missing/empty value handling (clean UNRESOLVED without querying 'None' or 'nan')
        Never hardcodes IDs or table mappings.
        """
        parent_keys = [parent_table, parent_table.lower(), parent_table.rstrip("s").lower()]
        db_records = db_records or []

        # 1. User Override takes top priority
        if user_override_id is not None:
            override_str = str(user_override_id).strip()
            matched_rec = {}
            for r in db_records:
                if str(r.get(parent_id_column)).strip() == override_str:
                    matched_rec = r
                    break
            res = ResolvedEntityRecord(
                status="RESOLVED",
                matchedTable=parent_table,
                matchedPrimaryKeyColumn=parent_id_column,
                matchedPrimaryKeyValue=user_override_id,
                matchedBy=["USER_OVERRIDE"],
                confidence=1.0,
                recordData=matched_rec,
                matchMethod="USER_OVERRIDE",
                message=f"Resolved via user override to ID '{user_override_id}'."
            )
            if row_resolved_entities is not None:
                for pk in parent_keys:
                    row_resolved_entities[pk] = res
            return res

        # 2. Clean and sanitize source value
        s_val: Optional[str] = None
        if excel_source_value is not None and not is_empty_or_na(excel_source_value):
            raw_s = str(excel_source_value).strip()
            if raw_s and raw_s.lower() not in ("nan", "none", "null", "nat"):
                s_val = raw_s

        is_outlet_parent = parent_table.lower() in ("outlets", "dbo.outlets")
        outlet_cache_key = s_val
        if is_outlet_parent and isinstance(row_data, dict):
            outlet_cache_key = outlet_row_source_key(row_data)

        # Same-Row Entity Reuse Check: If an entity was already resolved for this row and
        # no source value or row data is provided (or row data lacks outlet fields), reuse it.
        existing_row_entity: Optional[ResolvedEntityRecord] = None
        if row_resolved_entities is not None:
            for pk in parent_keys:
                if pk in row_resolved_entities:
                    existing_row_entity = row_resolved_entities[pk]
                    break

        if existing_row_entity and existing_row_entity.status == "RESOLVED":
            # If current source value is empty/missing, check if row_data has competing outlet fields
            has_competing_outlet = False
            if is_outlet_parent and isinstance(row_data, dict):
                r_val, v_val = outlet_row_source_values(row_data)
                if r_val or v_val:
                    has_competing_outlet = True
            if s_val is None and not has_competing_outlet:
                return existing_row_entity

        # Outlet name fields are a row-level identity: both must be evaluated before
        # any cached or previously-resolved entity can be reused.
        if is_outlet_parent:
            outlet_res = OutletMatcher.resolve_row_outlet(
                row_data=row_data,
                db_records=db_records,
                id_col=parent_id_column,
                name_col=parent_name_column or "Outlet_Name"
            ) if isinstance(row_data, dict) else None
            if (not outlet_res or outlet_res.get("status") == "MISSING_NAME") and s_val is not None:
                outlet_res = OutletMatcher.match_outlet_name(
                    source_name=s_val,
                    db_records=db_records,
                    id_col=parent_id_column,
                    name_col=parent_name_column or "Outlet_Name"
                )

            outlet_status = outlet_res.get("status", "UNRESOLVED") if outlet_res else "UNRESOLVED"
            candidates = outlet_res.get("candidates", []) if outlet_res else []
            candidate_ids = sorted({str(c.get("id")) for c in candidates if c.get("id") is not None})
            restaurant_value = outlet_res.get("restaurant_value") if outlet_res else None
            virtual_brand_value = outlet_res.get("virtual_brand_value") if outlet_res else None
            logger.info(
                "[OUTLET_FK_RESOLUTION] upload_id=%s source_row=%s restaurant_name=%r virtual_brand_name=%r "
                "normalized_key=%r parent=%s referenced_key=%s candidate_count=%d candidate_ids=%s "
                "status=%s resolved_id=%s reason=%r",
                upload_id or "unknown",
                source_row_num if source_row_num is not None else "unknown",
                restaurant_value,
                virtual_brand_value,
                outlet_res.get("normalized_name") if outlet_res else None,
                parent_table,
                parent_id_column,
                len(candidate_ids),
                candidate_ids,
                outlet_status,
                outlet_res.get("outlet_id") if outlet_res else None,
                outlet_res.get("message") if outlet_res else "No outlet candidates were loaded."
            )

            if outlet_status == "MATCHED":
                matched_id = outlet_res.get("outlet_id")
                matched_rec = next(
                    (r for r in db_records if str(r.get(parent_id_column)) == str(matched_id)),
                    None
                )
                if matched_id is None or matched_rec is None:
                    outlet_status = "UNRESOLVED"
                    outlet_res["message"] = "Resolved outlet ID is absent from the loaded referenced records."
                else:
                    result = ResolvedEntityRecord(
                        status="RESOLVED",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=matched_id,
                        matchedBy=[
                            value for value in (
                                f"restaurant_name={restaurant_value}" if restaurant_value else None,
                                f"virtual_brand_name={virtual_brand_value}" if virtual_brand_value else None
                            ) if value
                        ],
                        confidence=outlet_res.get("confidence", 0.95),
                        recordData=matched_rec,
                        matchMethod=outlet_res.get("match_type", "OUTLET_MATCHER"),
                        message=outlet_res.get("message")
                    )
                    if row_resolved_entities is not None:
                        for key in parent_keys:
                            row_resolved_entities[key] = result
                    if migration_cache is not None:
                        migration_cache[(parent_table, outlet_cache_key)] = result
                    return result

            status = outlet_status if outlet_status in ("AMBIGUOUS", "CONFLICTING_OUTLET_VALUES") else "UNRESOLVED"
            msg = outlet_res.get("message") if outlet_res else None
            if not msg or outlet_res.get("status") == "MISSING_NAME" or (not row_data and not s_val):
                msg = f"Required foreign key '{child_column}' missing identifying name in source data."
            return ResolvedEntityRecord(
                status=status,
                matchedTable=parent_table,
                matchedPrimaryKeyColumn=parent_id_column,
                matchedPrimaryKeyValue=None,
                matchedBy=[str(v) for v in (restaurant_value, virtual_brand_value) if v],
                confidence=outlet_res.get("confidence", 0.0) if outlet_res else 0.0,
                candidates=candidates,
                matchMethod=outlet_res.get("match_type", status) if outlet_res else status,
                message=msg
            )

        # 3. Same-Row Entity Reuse Check
        existing_row_entity: Optional[ResolvedEntityRecord] = None
        if row_resolved_entities is not None:
            for pk in parent_keys:
                if pk in row_resolved_entities:
                    existing_row_entity = row_resolved_entities[pk]
                    break

        if existing_row_entity and existing_row_entity.status == "RESOLVED":
            # If current source value is empty/missing, reuse the resolved parent entity for this row
            if s_val is None:
                return existing_row_entity
            # If current source value matches the entity already resolved for this row
            existing_name = str(existing_row_entity.record_data.get(parent_name_column or "") or "").strip()
            if (
                s_val in existing_row_entity.matched_by
                or (existing_name and (normalize_string(s_val) == normalize_string(existing_name) or s_val.lower() == existing_name.lower()))
            ):
                return existing_row_entity
            # If s_val is non-empty and different (e.g. assign_to differs), proceed to independent resolution below

        # 4. Check migration-level cache if s_val is present
        is_item_parent = is_item_master_target(parent_table, child_table, child_column)
        if migration_cache is not None and s_val is not None:
            if is_outlet_parent:
                cache_key = (parent_table, outlet_cache_key)
            elif is_item_parent:
                cache_key = (parent_table, item_row_source_key(row_data if isinstance(row_data, dict) else {}, s_val))
            else:
                cache_key = (parent_table, s_val)

            if cache_key in migration_cache:
                cached = migration_cache[cache_key]
                if cached.status == "RESOLVED":
                    if row_resolved_entities is not None:
                        for pk in parent_keys:
                            row_resolved_entities[pk] = cached
                    return cached

        # 5. Direct matching against parent_table via TableLookupIndex
        if s_val is not None and db_records:
            if index is None:
                index = cls.get_or_create_index(db_records, parent_id_column, parent_name_column or parent_id_column)

            # 5a. Exact match
            exact_matches = index.exact_map.get(s_val, [])
            if len(exact_matches) == 1:
                # If this is an item master lookup and row_data has category/group, verify compatibility
                rec_entry = exact_matches[0]
                is_compatible = True
                if is_item_parent and row_data:
                    # Check if candidate has classification data that directly contradicts row_data
                    filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                        candidates=[{"id": rec_entry["id"], "name": rec_entry["name"], "record": rec_entry.get("record", {})}],
                        row_data=row_data,
                        all_parent_records=all_parent_records
                    )
                    # If disambiguation ran and filtered it out, do not falsely resolve
                    if not filt_cands:
                        is_compatible = False

                if is_compatible:
                    res = ResolvedEntityRecord(
                        status="RESOLVED",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=rec_entry["id"],
                        matchedBy=[s_val],
                        confidence=1.0,
                        recordData=rec_entry.get("record", {}),
                        matchMethod="DATABASE_LOOKUP",
                        message=f"Exact match on '{rec_entry['name']}' in table '{parent_table}'."
                    )
                    if row_resolved_entities is not None:
                        for pk in parent_keys:
                            row_resolved_entities[pk] = res
                    if migration_cache is not None:
                        if is_item_parent:
                            migration_cache[(parent_table, item_row_source_key(row_data if isinstance(row_data, dict) else {}, s_val))] = res
                        else:
                            migration_cache[(parent_table, s_val)] = res
                    return res
            elif len(exact_matches) > 1:
                seen_cands = {}
                for m in exact_matches:
                    seen_cands[m["id"]] = {"id": m["id"], "name": m["name"], "record": m.get("record", {})}
                cands_list = list(seen_cands.values())
                if len(cands_list) > 1:
                    # Attempt composite disambiguation using category, group, code, variant
                    if row_data:
                        filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                            candidates=cands_list,
                            row_data=row_data,
                            all_parent_records=all_parent_records
                        )
                        if len(filt_cands) == 1:
                            rec_entry = filt_cands[0]
                            res = ResolvedEntityRecord(
                                status="RESOLVED",
                                matchedTable=parent_table,
                                matchedPrimaryKeyColumn=parent_id_column,
                                matchedPrimaryKeyValue=rec_entry["id"],
                                matchedBy=[s_val],
                                confidence=0.98,
                                recordData=rec_entry.get("record", {}),
                                matchMethod=m_method or "COMPOSITE_LOOKUP",
                                message=m_expl or f"Disambiguated exact match on '{rec_entry['name']}' in table '{parent_table}'."
                            )
                            if row_resolved_entities is not None:
                                for pk in parent_keys:
                                    row_resolved_entities[pk] = res
                            return res
                        elif len(filt_cands) > 1:
                            cands_list = filt_cands

                    return ResolvedEntityRecord(
                        status="AMBIGUOUS",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=None,
                        matchedBy=[s_val],
                        confidence=0.5,
                        candidates=[{"id": c["id"], "name": c["name"]} for c in cands_list],
                        matchMethod="AMBIGUOUS",
                        message=f"Ambiguous: Name '{s_val}' matched {len(cands_list)} records in '{parent_table}'."
                    )
                else:
                    rec_entry = exact_matches[0]
                    res = ResolvedEntityRecord(
                        status="RESOLVED",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=rec_entry["id"],
                        matchedBy=[s_val],
                        confidence=1.0,
                        recordData=rec_entry.get("record", {}),
                        matchMethod="DATABASE_LOOKUP"
                    )
                    if row_resolved_entities is not None:
                        for pk in parent_keys:
                            row_resolved_entities[pk] = res
                    return res

            # 5b. Normalized match
            norm_val = normalize_cell_value(s_val)
            norm_matches = index.norm_map.get(norm_val) or index.norm_map.get(s_val.lower(), [])
            if norm_matches:
                seen_cands = {}
                for m in norm_matches:
                    seen_cands[m["id"]] = {"id": m["id"], "name": m["name"], "record": m.get("record", {})}
                cands_list = list(seen_cands.values())
                if len(cands_list) == 1:
                    rec_entry = cands_list[0]
                    res = ResolvedEntityRecord(
                        status="RESOLVED",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=rec_entry["id"],
                        matchedBy=[s_val],
                        confidence=0.95,
                        recordData=rec_entry.get("record", {}),
                        matchMethod="DATABASE_LOOKUP",
                        message=f"Normalized match on '{rec_entry['name']}' in table '{parent_table}'."
                    )
                    if row_resolved_entities is not None:
                        for pk in parent_keys:
                            row_resolved_entities[pk] = res
                    if migration_cache is not None:
                        if is_item_parent:
                            migration_cache[(parent_table, item_row_source_key(row_data if isinstance(row_data, dict) else {}, s_val))] = res
                        else:
                            migration_cache[(parent_table, s_val)] = res
                    return res
                elif len(cands_list) > 1:
                    if row_data:
                        filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                            candidates=cands_list,
                            row_data=row_data,
                            all_parent_records=all_parent_records
                        )
                        if len(filt_cands) == 1:
                            rec_entry = filt_cands[0]
                            res = ResolvedEntityRecord(
                                status="RESOLVED",
                                matchedTable=parent_table,
                                matchedPrimaryKeyColumn=parent_id_column,
                                matchedPrimaryKeyValue=rec_entry["id"],
                                matchedBy=[s_val],
                                confidence=0.95,
                                recordData=rec_entry.get("record", {}),
                                matchMethod=m_method or "COMPOSITE_LOOKUP",
                                message=m_expl or f"Disambiguated normalized match on '{rec_entry['name']}' in table '{parent_table}'."
                            )
                            if row_resolved_entities is not None:
                                for pk in parent_keys:
                                    row_resolved_entities[pk] = res
                            if migration_cache is not None:
                                if is_item_parent:
                                    migration_cache[(parent_table, item_row_source_key(row_data if isinstance(row_data, dict) else {}, s_val))] = res
                                else:
                                    migration_cache[(parent_table, s_val)] = res
                            return res
                        elif len(filt_cands) > 1:
                            cands_list = filt_cands

                    return ResolvedEntityRecord(
                        status="AMBIGUOUS",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=None,
                        matchedBy=[s_val],
                        confidence=0.5,
                        candidates=[{"id": c["id"], "name": c["name"]} for c in cands_list],
                        matchMethod="AMBIGUOUS",
                        message=f"Ambiguous: Normalized name '{s_val}' matched {len(cands_list)} records in '{parent_table}'."
                    )

            # 5c. Explicit item portion/spelling aliases. The general FK resolver
            # previously stopped after exact/normalized matching, so semantic aliases
            # implemented by match_name_to_id were never used by the actual insert path.
            # Keep this restricted to item master foreign keys and the explicit alias
            # rules; never use arbitrary fuzzy matching to assign an Item_ID.
            is_item_master_fk = is_item_parent
            if is_item_master_fk and s_val is not None:
                resolved_name_col = parent_name_column
                if not resolved_name_col:
                    for candidate_key in db_records[0].keys():
                        key_norm = normalize_column_name(str(candidate_key))
                        if key_norm in ("item", "item name", "product", "product name", "description"):
                            resolved_name_col = str(candidate_key)
                            break
                if resolved_name_col:
                    alias_matches = cls._variant_alias_candidates(
                        source_name=s_val,
                        db_records=db_records,
                        parent_id_column=parent_id_column,
                        parent_name_column=resolved_name_col,
                        row_data=row_data,
                        all_parent_records=all_parent_records,
                    )
                    if len(alias_matches) == 1:
                        candidate = alias_matches[0]
                        res = ResolvedEntityRecord(
                            status="RESOLVED",
                            matchedTable=parent_table,
                            matchedPrimaryKeyColumn=parent_id_column,
                            matchedPrimaryKeyValue=candidate["id"],
                            matchedBy=[s_val, candidate["name"]],
                            confidence=0.92,
                            recordData=candidate.get("record", {}),
                            matchMethod="SEMANTIC_VARIANT_ALIAS",
                            message=f"Resolved item '{s_val}' to existing master item '{candidate['name']}' using validated semantic variant rules."
                        )
                        if row_resolved_entities is not None:
                            for key in parent_keys:
                                row_resolved_entities[key] = res
                        if migration_cache is not None:
                            if is_item_parent:
                                migration_cache[(parent_table, item_row_source_key(row_data if isinstance(row_data, dict) else {}, s_val))] = res
                            else:
                                migration_cache[(parent_table, s_val)] = res
                        return res
                    if len(alias_matches) > 1:
                        return ResolvedEntityRecord(
                            status="AMBIGUOUS",
                            matchedTable=parent_table,
                            matchedPrimaryKeyColumn=parent_id_column,
                            matchedPrimaryKeyValue=None,
                            matchedBy=[s_val],
                            confidence=0.5,
                            candidates=[{"id": c["id"], "name": c["name"]} for c in alias_matches[:10]],
                            matchMethod="AMBIGUOUS_VARIANT_ALIAS",
                            message=f"Multiple existing master items are compatible with '{s_val}'; refusing to guess."
                        )

            # 5d. Token / substring fuzzy matches
            # For item master, never guess or arbitrarily assign an Item_ID from token fuzzy match
            if not is_item_parent:
                fuzzy_matches = []
                fuzzy_ids = set()
                for tok in norm_val.split():
                    if len(tok) >= 3 and tok in index.token_map:
                        for c in index.token_map[tok]:
                            if c["id"] not in fuzzy_ids:
                                fuzzy_ids.add(c["id"])
                                fuzzy_matches.append(c)
                                if len(fuzzy_matches) >= 5:
                                    break
                    if len(fuzzy_matches) >= 5:
                        break

                if fuzzy_matches:
                    if row_data and len(fuzzy_matches) > 1:
                        filt_cands, m_method, m_expl = cls.disambiguate_candidates_by_composite_attributes(
                            candidates=fuzzy_matches,
                            row_data=row_data,
                            all_parent_records=all_parent_records
                        )
                        if len(filt_cands) == 1:
                            rec_entry = filt_cands[0]
                            res = ResolvedEntityRecord(
                                status="RESOLVED",
                                matchedTable=parent_table,
                                matchedPrimaryKeyColumn=parent_id_column,
                                matchedPrimaryKeyValue=rec_entry["id"],
                                matchedBy=[s_val],
                                confidence=0.90,
                                recordData=rec_entry.get("record", {}),
                                matchMethod=m_method or "COMPOSITE_LOOKUP",
                                message=m_expl or f"Disambiguated token match on '{rec_entry['name']}' in table '{parent_table}'."
                            )
                            if row_resolved_entities is not None:
                                for pk in parent_keys:
                                    row_resolved_entities[pk] = res
                            return res
                        elif len(filt_cands) > 1:
                            fuzzy_matches = filt_cands

                    return ResolvedEntityRecord(
                        status="AMBIGUOUS",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=None,
                        matchedBy=[s_val],
                        confidence=0.5,
                        candidates=[{"id": c["id"], "name": c["name"]} for c in fuzzy_matches[:5]],
                        matchMethod="AMBIGUOUS",
                        message=f"Ambiguous: Name '{s_val}' matched multiple candidates ({len(fuzzy_matches)}) in '{parent_table}'."
                    )
        if row_value_mappings:
            for vm in row_value_mappings:
                # 6a. Direct value mapping match targeting parent_table
                if vm.matched_table and vm.matched_table.lower() == parent_table.lower() and vm.matched_primary_key_value is not None:
                    matched_pk = vm.matched_primary_key_value
                    matched_rec = vm.matched_record_data or {}
                    if not matched_rec and db_records:
                        for r in db_records:
                            if r.get(parent_id_column) == matched_pk:
                                matched_rec = r
                                break
                    res = ResolvedEntityRecord(
                        status="RESOLVED",
                        matchedTable=parent_table,
                        matchedPrimaryKeyColumn=parent_id_column,
                        matchedPrimaryKeyValue=matched_pk,
                        matchedBy=[f"ValueMapping: {vm.excel_column}={vm.excel_value}"],
                        confidence=vm.confidence,
                        recordData=matched_rec,
                        matchMethod="VALUE_MAPPING",
                        message=f"Resolved via value mapping from {vm.excel_column} to {vm.target_value}."
                    )
                    if row_resolved_entities is not None:
                        for pk in parent_keys:
                            row_resolved_entities[pk] = res
                    return res

                # 6b. Correlated value mapping on a related entity record (e.g. Customer.Outlet_ID)
                if vm.matched_record_data:
                    for k, v in vm.matched_record_data.items():
                        if k.lower() == parent_id_column.lower() and v is not None:
                            matched_outlet_id = v
                            matched_outlet_rec = {}
                            if db_records:
                                for r in db_records:
                                    if r.get(parent_id_column) == matched_outlet_id:
                                        matched_outlet_rec = r
                                        break
                            res = ResolvedEntityRecord(
                                status="RESOLVED",
                                matchedTable=parent_table,
                                matchedPrimaryKeyColumn=parent_id_column,
                                matchedPrimaryKeyValue=matched_outlet_id,
                                matchedBy=[f"ValueMapping {vm.database_table}.{vm.database_column}: {vm.target_value}"],
                                confidence=0.95,
                                recordData=matched_outlet_rec,
                                matchMethod="VALUE_MAPPING_CORRELATED",
                                message=f"Resolved via correlated value mapping on {vm.database_table}.{vm.database_column} -> {vm.target_value} ({parent_id_column}={matched_outlet_id})."
                            )
                            if row_resolved_entities is not None:
                                for pk in parent_keys:
                                    row_resolved_entities[pk] = res
                            return res

        # 7. Correlated entity inspection from row_data and related parent tables (e.g. Customer Location)
        if all_parent_records and parent_table.lower() in ("outlets", "dbo.outlets"):
            cust_records = all_parent_records.get("Customer", [])
            loc_candidates = []
            if row_data:
                for k in ("Location", "location", "restaurant_name", "restaurant", "area", "address"):
                    if k in row_data and row_data[k] and not is_empty_or_na(row_data[k]):
                        loc_candidates.append(str(row_data[k]).strip())

            if loc_candidates and cust_records:
                for cand_loc in loc_candidates:
                    norm_c = normalize_string(cand_loc)
                    for cr in cust_records:
                        cr_loc = str(cr.get("Location") or "").strip()
                        if cr_loc and (cr_loc.lower() == cand_loc.lower() or normalize_string(cr_loc) == norm_c):
                            out_id = cr.get("Outlet_ID")
                            if out_id is not None:
                                matched_out_rec = {}
                                if db_records:
                                    for r in db_records:
                                        if r.get(parent_id_column) == out_id:
                                            matched_out_rec = r
                                            break
                                res = ResolvedEntityRecord(
                                    status="RESOLVED",
                                    matchedTable=parent_table,
                                    matchedPrimaryKeyColumn=parent_id_column,
                                    matchedPrimaryKeyValue=out_id,
                                    matchedBy=[f"Customer.Location: {cand_loc}"],
                                    confidence=0.95,
                                    recordData=matched_out_rec,
                                    matchMethod="CORRELATED_LOOKUP",
                                    message=f"Resolved via customer location '{cand_loc}' referencing {parent_id_column}={out_id}."
                                )
                                if row_resolved_entities is not None:
                                    for pk in parent_keys:
                                        row_resolved_entities[pk] = res
                                return res

        # 8. Missing source value
        if s_val is None:
            return ResolvedEntityRecord(
                status="UNRESOLVED",
                matchedTable=parent_table,
                matchedPrimaryKeyColumn=parent_id_column,
                matchedPrimaryKeyValue=None,
                matchMethod="MISSING_VALUE",
                message=f"Required foreign key '{child_column}' missing identifying name in source data."
            )

        # 9. Value not found
        return ResolvedEntityRecord(
            status="UNRESOLVED",
            matchedTable=parent_table,
            matchedPrimaryKeyColumn=parent_id_column,
            matchedPrimaryKeyValue=None,
            matchedBy=[s_val],
            matchMethod="UNRESOLVED",
            message=f"Referenced value '{s_val}' not found in database table '{parent_table}'."
        )

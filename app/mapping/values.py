import os
import json
import threading
import time
from typing import Dict, List, Any, Optional, Set, Tuple
import pandas as pd
from sqlalchemy import text, Engine
from app.utils.normalization import normalize_cell_value, tokenize_cell_value
from app.models.migration import ValueMappingItem
from app.core.logging import logger


DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
SYNONYMS_FILE = os.path.join(DATA_DIR, "value_synonyms.json")
CACHE_FILE = os.path.join(DATA_DIR, "value_mappings_cache.json")


# Default Seeded Value Synonyms
DEFAULT_VALUE_SYNONYMS: Dict[str, Dict[str, List[str]]] = {
    "Order Type": {
        "Dine In": ["Dinning", "Dining", "Dine In", "Dine-In", "DineIn", "Eat In", "Table"],
        "Take Away": ["TakeAway", "Take Away", "Take-Away", "take_away", "Takeout", "Take Out", "Carry Out", "Pick Up", "Pickup"],
        "Home Delivery": ["HomeDelivery", "Home Delivery", "Home-Delivery", "home_delivery", "Door Delivery", "Delivery"],
        "Parcel": ["Parcel", "Packing", "Package"]
    },
    "Payment Mode": {
        "Cash": ["cash", "cod", "cash on delivery"],
        "Credit Card": ["card", "credit card", "cc", "debit card"],
        "UPI": ["upi", "gpay", "phonepe", "paytm", "online"]
    },
    "Order Status": {
        "Completed": ["completed", "done", "delivered", "closed", "settled"],
        "Cancelled": ["cancelled", "canceled", "void", "rejected"],
        "Pending": ["pending", "new", "in progress", "processing", "open"]
    }
}

# Known default valid values by column name (for offline testing or empty initial tables)
DEFAULT_COLUMN_VALID_VALUES: Dict[str, List[str]] = {
    "order_type": ["Dine In", "Take Away", "Home Delivery", "Parcel"],
    "ordertype": ["Dine In", "Take Away", "Home Delivery", "Parcel"],
    "order_status": ["Completed", "Cancelled", "Pending"],
    "status": ["Active", "Inactive", "Completed", "Cancelled", "Pending"],
    "payment_mode": ["Cash", "Credit Card", "UPI"],
}


class ValueSynonymRegistry:
    """
    Configurable, thread-safe value synonym registry and approved mappings cache.
    Persists synonyms in `data/value_synonyms.json` and column-scoped approved mappings in `data/value_mappings_cache.json`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._synonyms: Dict[str, Dict[str, List[str]]] = self._load_synonyms()
        self._cache: Dict[str, Dict[str, str]] = self._load_cache()

    def _load_synonyms(self) -> Dict[str, Dict[str, List[str]]]:
        res = {cat: {k: list(v) for k, v in targets.items()} for cat, targets in DEFAULT_VALUE_SYNONYMS.items()}
        if os.path.exists(SYNONYMS_FILE):
            try:
                with open(SYNONYMS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for cat, targets in data.items():
                            if cat not in res:
                                res[cat] = {}
                            if isinstance(targets, dict):
                                for tgt, syns in targets.items():
                                    if tgt not in res[cat]:
                                        res[cat][tgt] = []
                                    if isinstance(syns, list):
                                        for s in syns:
                                            if s not in res[cat][tgt]:
                                                res[cat][tgt].append(s)
                        return res
            except Exception as e:
                logger.warning(f"[VALUE_REGISTRY] Failed to load synonyms from {SYNONYMS_FILE}: {e}")
        return res

    def _save_synonyms(self):
        try:
            with open(SYNONYMS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._synonyms, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[VALUE_REGISTRY] Failed to save synonyms to {SYNONYMS_FILE}: {e}")

    def _load_cache(self) -> Dict[str, Dict[str, str]]:
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                logger.warning(f"[VALUE_REGISTRY] Failed to load cache from {CACHE_FILE}: {e}")
        return {}

    def _save_cache(self):
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[VALUE_REGISTRY] Failed to save cache to {CACHE_FILE}: {e}")

    @staticmethod
    def _scope_key(table: str, column: str, database_scope: Optional[str] = None) -> str:
        clean_tbl = str(table).strip().lower()
        clean_col = str(column).strip().lower()
        if database_scope:
            return f"{str(database_scope).strip().lower()}.{clean_tbl}.{clean_col}"
        return f"{clean_tbl}.{clean_col}"

    def get_synonyms(self) -> Dict[str, Dict[str, List[str]]]:
        with self._lock:
            return {cat: {k: list(v) for k, v in targets.items()} for cat, targets in self._synonyms.items()}

    def add_synonym(self, category: str, target_value: str, synonym: str):
        with self._lock:
            if category not in self._synonyms:
                self._synonyms[category] = {}
            if target_value not in self._synonyms[category]:
                self._synonyms[category][target_value] = []
            clean_syn = str(synonym).strip()
            if clean_syn and clean_syn not in self._synonyms[category][target_value]:
                self._synonyms[category][target_value].append(clean_syn)
            self._save_synonyms()
        logger.info(f"[VALUE_REGISTRY] Added synonym: '{synonym}' -> '{target_value}' in '{category}'")

    def remove_synonym(self, category: str, target_value: str, synonym: str):
        with self._lock:
            if category in self._synonyms and target_value in self._synonyms[category]:
                clean_syn = str(synonym).strip()
                self._synonyms[category][target_value] = [
                    s for s in self._synonyms[category][target_value] if s.lower() != clean_syn.lower()
                ]
                self._save_synonyms()
        logger.info(f"[VALUE_REGISTRY] Removed synonym: '{synonym}' from '{target_value}' in '{category}'")

    def find_synonym_target(self, raw_value: str) -> Optional[str]:
        """
        Looks up if raw_value (or its normalized form) matches any registered synonym.
        """
        norm_val = normalize_cell_value(raw_value)
        with self._lock:
            for cat, targets in self._synonyms.items():
                for target_val, syn_list in targets.items():
                    if normalize_cell_value(target_val) == norm_val:
                        return target_val
                    for syn in syn_list:
                        if normalize_cell_value(syn) == norm_val:
                            return target_val
        return None

    def find_all_synonym_targets(self, raw_value: str) -> List[str]:
        """
        Returns all target values that register the raw_value as a synonym or token match.
        """
        norm_val = normalize_cell_value(raw_value)
        tokens = set(tokenize_cell_value(raw_value))
        matched_targets: List[str] = []

        with self._lock:
            for cat, targets in self._synonyms.items():
                for target_val, syn_list in targets.items():
                    # Exact or normalized match to target
                    if normalize_cell_value(target_val) == norm_val:
                        if target_val not in matched_targets:
                            matched_targets.append(target_val)
                    # Check synonyms
                    for syn in syn_list:
                        norm_syn = normalize_cell_value(syn)
                        if norm_syn == norm_val or (norm_syn in tokens and len(norm_syn) >= 3):
                            if target_val not in matched_targets:
                                matched_targets.append(target_val)
        return matched_targets

    def save_approved_mapping(
        self,
        table: str,
        column: str,
        source_value: str,
        target_value: str,
        database_scope: Optional[str] = None
    ):
        """
        Caches an approved mapping for future imports, scoped to the target database column.
        """
        key = self._scope_key(table, column, database_scope)
        unscoped_key = self._scope_key(table, column, None)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = {}
            self._cache[key][str(source_value).strip()] = str(target_value).strip()
            # Also store under unscoped key for convenience
            if unscoped_key not in self._cache:
                self._cache[unscoped_key] = {}
            self._cache[unscoped_key][str(source_value).strip()] = str(target_value).strip()
            self._save_cache()
        logger.info(f"[VALUE_CACHE] Cached approved mapping for {key}: '{source_value}' -> '{target_value}'")

    def get_approved_mappings(
        self,
        table: str,
        column: str,
        database_scope: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Retrieves cached approved mappings for a specific table and column.
        """
        key = self._scope_key(table, column, database_scope)
        unscoped_key = self._scope_key(table, column, None)
        with self._lock:
            mappings = dict(self._cache.get(unscoped_key, {}))
            if database_scope and key in self._cache:
                mappings.update(self._cache[key])
            return mappings

    def get_all_cached_mappings(self) -> List[Dict[str, Any]]:
        with self._lock:
            res = []
            for scope, map_dict in self._cache.items():
                parts = scope.split(".")
                tbl = parts[-2] if len(parts) >= 2 else scope
                col = parts[-1] if len(parts) >= 1 else scope
                for src, tgt in map_dict.items():
                    res.append({
                        "scope": scope,
                        "databaseTable": tbl,
                        "databaseColumn": col,
                        "sourceValue": src,
                        "targetValue": tgt
                    })
            return res

    def delete_cached_mapping(self, table: str, column: str, source_value: str, database_scope: Optional[str] = None):
        key = self._scope_key(table, column, database_scope)
        unscoped_key = self._scope_key(table, column, None)
        with self._lock:
            if key in self._cache:
                self._cache[key].pop(str(source_value).strip(), None)
            if unscoped_key in self._cache:
                self._cache[unscoped_key].pop(str(source_value).strip(), None)
            self._save_cache()


value_registry = ValueSynonymRegistry()


class ValueMatcher:
    """
    Intelligent Value Matching Engine.
    Matches Excel cell values with valid values from the mapped database column.
    Architecture:
    - Layer 0: User Override / Scoped Cached Approved Mapping (1.0)
    - Layer 1: Exact Match (1.0)
    - Layer 2: Normalized Match ignoring capitalization, spaces, hyphens, punctuation (0.95)
    - Layer 3: Configurable Synonym Match verified against valid DB values (0.90)
    - Layer 4: Multi-Candidate Ambiguity Detection (e.g. 'Delivery(Parcel)' matches 'Parcel' and 'Home Delivery' -> AMBIGUOUS) (0.50)
    - Layer 5: Unmatched (0.0)
    """

    @staticmethod
    def get_valid_column_values(
        engine: Optional[Engine],
        table: str,
        column: str,
        known_valid_values: Optional[List[str]] = None
    ) -> List[str]:
        """
        Inspects valid values for a target database column:
        1. Queries distinct non-null values from the connected database table if engine is available.
        2. Falls back to or includes known categorical default values.
        """
        valid_set: Set[str] = set()

        if known_valid_values:
            for v in known_valid_values:
                if str(v).strip():
                    valid_set.add(str(v).strip())

        # Check default categorical lists
        col_clean = column.lower().replace(" ", "_")
        if col_clean in DEFAULT_COLUMN_VALID_VALUES:
            for v in DEFAULT_COLUMN_VALID_VALUES[col_clean]:
                valid_set.add(v)

        if engine is not None:
            dialect_name = getattr(engine, "name", "mssql")
            clean_tbl = str(table).strip().replace("[", "").replace("]", "").replace("`", "").replace('"', '')
            clean_col = str(column).strip().replace("[", "").replace("]", "").replace("`", "").replace('"', '')

            if dialect_name == "mssql":
                q_tbl = f"[{clean_tbl}]"
                q_col = f"[{clean_col}]"
                sql = f"SELECT DISTINCT TOP 500 {q_col} FROM {q_tbl} WHERE {q_col} IS NOT NULL AND LTRIM(RTRIM(CAST({q_col} AS NVARCHAR(MAX)))) != ''"
            elif dialect_name in ("mysql", "mariadb"):
                sql = f"SELECT DISTINCT `{clean_col}` FROM `{clean_tbl}` WHERE `{clean_col}` IS NOT NULL AND TRIM(`{clean_col}`) != '' LIMIT 500"
            else:
                sql = f'SELECT DISTINCT "{clean_col}" FROM "{clean_tbl}" WHERE "{clean_col}" IS NOT NULL AND TRIM("{clean_col}") != \'\' LIMIT 500'

            try:
                with engine.connect() as conn:
                    res = conn.execute(text(sql)).scalars().all()
                    for val in res:
                        if val is not None:
                            s = str(val).strip()
                            if s and s.lower() not in ("nan", "none", "null", "nat"):
                                valid_set.add(s)
            except Exception as e:
                logger.debug(f"[VALUE_MATCHER] Could not query distinct values for {table}.{column}: {e}")

        return sorted(list(valid_set))

    @staticmethod
    def match_single_value(
        excel_value: str,
        valid_db_values: List[str],
        target_table: str,
        target_column: str,
        user_overrides: Optional[Dict[str, str]] = None,
        cached_mappings: Optional[Dict[str, str]] = None,
        database_scope: Optional[str] = None
    ) -> Tuple[Optional[str], str, str, float, str, List[Dict[str, Any]]]:
        """
        Matches a single Excel cell value against valid database column values.
        Returns:
            (target_value, match_type, status, confidence, explanation, candidates)
        """
        raw_val = str(excel_value).strip() if excel_value is not None else ""
        if not raw_val or raw_val.lower() in ("nan", "none", "null", "nat"):
            return None, "EMPTY", "SKIPPED", 1.0, "Empty value", []

        norm_val = normalize_cell_value(raw_val)
        val_tokens = set(tokenize_cell_value(raw_val))

        # -------------------------------------------------------------
        # Layer 0: User Override / Scoped Cached Approved Mapping (1.0)
        # -------------------------------------------------------------
        if user_overrides and raw_val in user_overrides:
            override_val = user_overrides[raw_val]
            return (
                override_val,
                "USER_OVERRIDE",
                "MATCHED",
                1.0,
                f"User override applied: '{raw_val}' -> '{override_val}'.",
                []
            )

        if cached_mappings:
            if raw_val in cached_mappings:
                cached_val = cached_mappings[raw_val]
                return (
                    cached_val,
                    "APPROVED",
                    "MATCHED",
                    1.0,
                    f"Matched via cached approved mapping: '{raw_val}' -> '{cached_val}'.",
                    []
                )
            for c_src, c_tgt in cached_mappings.items():
                if normalize_cell_value(c_src) == norm_val:
                    return (
                        c_tgt,
                        "APPROVED",
                        "MATCHED",
                        1.0,
                        f"Matched via cached approved mapping: '{raw_val}' -> '{c_tgt}'.",
                        []
                    )

        # Build normalized index for valid database values
        db_norm_map: Dict[str, str] = {}
        for db_v in valid_db_values:
            db_norm_map[normalize_cell_value(db_v)] = db_v

        # -------------------------------------------------------------
        # Layer 1: Exact Match (1.0)
        # -------------------------------------------------------------
        if raw_val in valid_db_values:
            return (
                raw_val,
                "EXACT",
                "MATCHED",
                1.0,
                f"Exact match with valid database value: '{raw_val}'.",
                []
            )

        # -------------------------------------------------------------
        # Layer 2: Normalized Match (0.95)
        # Handles capitalization, spaces, hyphens, underscores, punctuation
        # E.g. 'TakeAway', 'Take-Away', 'take_away' -> 'Take Away'
        # E.g. 'HomeDelivery', 'Home-Delivery' -> 'Home Delivery'
        # -------------------------------------------------------------
        if norm_val in db_norm_map:
            matched_db_v = db_norm_map[norm_val]
            return (
                matched_db_v,
                "NORMALIZED",
                "MATCHED",
                0.95,
                f"Normalized match ('{raw_val}' -> '{matched_db_v}') ignoring case, spaces, and punctuation.",
                []
            )

        # -------------------------------------------------------------
        # Layer 3: Configurable Synonym Match (0.90)
        # E.g. 'Dinning' -> 'Dine In'
        # Verified against valid database values
        # -------------------------------------------------------------
        synonym_target = value_registry.find_synonym_target(raw_val)
        if synonym_target:
            # Check if synonym target is in valid database values (or normalizes to one)
            norm_syn = normalize_cell_value(synonym_target)
            if norm_syn in db_norm_map:
                verified_target = db_norm_map[norm_syn]
                return (
                    verified_target,
                    "SYNONYM",
                    "MATCHED",
                    0.90,
                    f"Configured synonym verified against database value: '{raw_val}' -> '{verified_target}'.",
                    []
                )
            elif not valid_db_values:
                # When valid_db_values not yet populated, return as suggestion
                return (
                    synonym_target,
                    "SYNONYM",
                    "SUGGESTED",
                    0.85,
                    f"Configured synonym suggestion: '{raw_val}' -> '{synonym_target}'.",
                    []
                )

        # -------------------------------------------------------------
        # Layer 4: Multi-Candidate Ambiguity Detection (0.50)
        # Requirement 5: For 'Delivery(Parcel)', do not automatically choose
        # 'Parcel' or 'Home Delivery' if both are valid candidates. Flag for review!
        # -------------------------------------------------------------
        candidate_matches: List[Dict[str, Any]] = []

        # Find all synonym targets related to the tokens
        all_possible_synonyms = value_registry.find_all_synonym_targets(raw_val)

        for db_v in valid_db_values:
            db_norm = normalize_cell_value(db_v)
            db_tokens = set(tokenize_cell_value(db_v))

            # 1. Check if db_v is in synonym targets
            is_synonym_cand = db_v in all_possible_synonyms or any(
                normalize_cell_value(s) == db_norm for s in all_possible_synonyms
            )

            # 2. Check token overlap
            overlap = val_tokens.intersection(db_tokens)

            # 3. Check substring token containment
            contains_token = any(t in val_tokens for t in db_tokens) or any(t in db_tokens for t in val_tokens)

            if is_synonym_cand or overlap or contains_token:
                score = 0.50
                reason = []
                if is_synonym_cand:
                    reason.append(f"Matches synonym concept '{db_v}'")
                    score = max(score, 0.60)
                if overlap:
                    reason.append(f"Shares token(s): {', '.join(sorted(list(overlap)))}")
                    score = max(score, 0.55)

                candidate_matches.append({
                    "value": db_v,
                    "confidence": round(score, 2),
                    "reason": "; ".join(reason) if reason else "Partial token overlap"
                })

        # Deduplicate candidates by value
        seen_cand = set()
        unique_cands = []
        for c in candidate_matches:
            if c["value"] not in seen_cand:
                seen_cand.add(c["value"])
                unique_cands.append(c)

        if len(unique_cands) >= 2:
            cand_names = [c["value"] for c in unique_cands]
            return (
                None,
                "AMBIGUOUS",
                "AMBIGUOUS",
                0.50,
                f"Ambiguous value: multiple valid database candidates ({cand_names}) match '{raw_val}'. Flagged for user review.",
                unique_cands
            )
        elif len(unique_cands) == 1:
            cand = unique_cands[0]
            return (
                cand["value"],
                "SYNONYM",
                "SUGGESTED",
                cand["confidence"],
                f"Single candidate suggestion: '{raw_val}' -> '{cand['value']}'.",
                unique_cands
            )

        # -------------------------------------------------------------
        # Layer 5: Unmatched (0.0)
        # -------------------------------------------------------------
        fallback_candidates = [
            {"value": v, "confidence": 0.1, "reason": "Available database value"}
            for v in valid_db_values[:6]
        ]
        return (
            None,
            "UNMATCHED",
            "UNMATCHED",
            0.0,
            f"Value '{raw_val}' could not be matched to any valid value in {target_table}.{target_column}.",
            fallback_candidates
        )

    @classmethod
    def match_column_values(
        cls,
        df: pd.DataFrame,
        excel_column: str,
        target_table: str,
        target_column: str,
        valid_db_values: List[str],
        user_overrides: Optional[Dict[str, str]] = None,
        database_scope: Optional[str] = None,
        db_records: Optional[List[Dict[str, Any]]] = None
    ) -> List[ValueMappingItem]:
        """
        Extracts distinct cell values from an Excel column and evaluates matches against valid database values.
        Returns a list of ValueMappingItem objects with counts and diagnostic metadata.
        """
        if excel_column not in df.columns:
            return []

        series = df[excel_column].dropna().astype(str).str.strip()
        series = series[~series.isin(["", "nan", "none", "null", "nat"])]

        if series.empty:
            return []

        cached_mappings = value_registry.get_approved_mappings(target_table, target_column, database_scope)
        value_counts = series.value_counts().to_dict()

        # Build index on db_records by target_column if provided
        target_rec_map: Dict[str, Dict[str, Any]] = {}
        if db_records:
            for r in db_records:
                col_v = r.get(target_column)
                if col_v is not None:
                    s_v = str(col_v).strip()
                    if s_v:
                        target_rec_map[s_v] = r
                        target_rec_map[normalize_cell_value(s_v)] = r

        results: List[ValueMappingItem] = []

        for val_str, count in value_counts.items():
            (
                target_val,
                match_type,
                status,
                conf,
                explanation,
                candidates
            ) = cls.match_single_value(
                excel_value=val_str,
                valid_db_values=valid_db_values,
                target_table=target_table,
                target_column=target_column,
                user_overrides=user_overrides,
                cached_mappings=cached_mappings,
                database_scope=database_scope
            )

            matched_rec = None
            matched_pk_col = None
            matched_pk_val = None
            if target_val and target_rec_map:
                matched_rec = target_rec_map.get(target_val) or target_rec_map.get(normalize_cell_value(target_val))
                if matched_rec:
                    # Identify primary key or first ID column in matched record
                    for k in matched_rec.keys():
                        if k.lower() in (f"{target_table.lower()}_id", f"{target_table.lower().rstrip('s')}_id", "id"):
                            matched_pk_col = k
                            matched_pk_val = matched_rec[k]
                            break
                    if not matched_pk_col and matched_rec:
                        for k in matched_rec.keys():
                            if k.lower().endswith("_id") or k.lower().endswith("id"):
                                matched_pk_col = k
                                matched_pk_val = matched_rec[k]
                                break

            results.append(ValueMappingItem(
                excelColumn=excel_column,
                databaseTable=target_table,
                databaseColumn=target_column,
                excelValue=val_str,
                normalizedExcelValue=normalize_cell_value(val_str),
                targetValue=target_val,
                matchType=match_type,
                status=status,
                confidence=conf,
                explanation=explanation,
                candidates=candidates,
                occurrenceCount=int(count),
                matchedTable=target_table if matched_rec else None,
                matchedPrimaryKeyColumn=matched_pk_col,
                matchedPrimaryKeyValue=matched_pk_val,
                matchedRecordData=matched_rec
            ))

        # Sort: AMBIGUOUS and UNMATCHED first, then by count descending
        order = {"AMBIGUOUS": 0, "UNMATCHED": 1, "SUGGESTED": 2, "MATCHED": 3}
        results.sort(key=lambda item: (order.get(item.status, 4), -item.occurrence_count))
        return results

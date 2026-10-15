from typing import Dict, Any, Optional, List, Tuple, Set
from app.mapping.matcher import normalize_string
from app.core.logging import logger


class IdResolverStore:
    """
    Centralized, fully generic ID resolution store for database migration.
    Connects Excel row identities and business record identities to actual database primary keys.
    Supports:
    - Business key mappings (exact string, normalized string, numeric forms)
    - Row-level mappings (source Excel row index -> resolved/generated primary key)
    - Composite/tuple key mappings (tuple of attributes -> primary key)
    - Recursive multi-level foreign-key resolution across table dependency hierarchies.
    """

    def __init__(self):
        # table_name -> Dict[source_key, db_id]
        self._mappings: Dict[str, Dict[str, Any]] = {}
        # table_name -> Dict[row_index, db_id]
        self._row_mappings: Dict[str, Dict[int, Any]] = {}
        # table_name -> Dict[tuple_key, db_id]
        self._composite_mappings: Dict[str, Dict[Tuple, Any]] = {}
        # table_name -> Set[db_id] (all distinct database IDs captured)
        self._distinct_ids: Dict[str, Set[Any]] = {}

    def register_id(self, table: str, source_key: Any, generated_id: Any):
        """
        Registers dynamic mapping between source business reference key and generated database ID.
        Stores exact string, normalized, and stripped variants.
        """
        if source_key is None or generated_id is None:
            return

        s_key = str(source_key).strip()
        if not s_key or s_key.lower() in ("nan", "none", "null", "nat"):
            return

        if table not in self._mappings:
            self._mappings[table] = {}
            self._distinct_ids[table] = set()

        self._mappings[table][s_key] = generated_id
        norm = normalize_string(s_key)
        if norm:
            self._mappings[table][norm] = generated_id
        lower = s_key.lower()
        if lower != s_key:
            self._mappings[table][lower] = generated_id

        # If numeric string ending in .0, also store integer string
        try:
            val_f = float(s_key)
            if val_f.is_integer():
                self._mappings[table][str(int(val_f))] = generated_id
        except (ValueError, TypeError):
            pass

        self._distinct_ids[table].add(generated_id)
        logger.debug(f"[ID_RESOLVER] {table}: '{s_key}' -> {generated_id}")

    def register_row_id(self, table: str, row_idx: int, generated_id: Any):
        """
        Maps a specific Excel row index to the generated or reused database primary key.
        Allows dependent child records on the same row to immediately resolve parent IDs.
        """
        if generated_id is None:
            return

        if table not in self._row_mappings:
            self._row_mappings[table] = {}
            self._distinct_ids.setdefault(table, set())

        self._row_mappings[table][int(row_idx)] = generated_id
        self._distinct_ids[table].add(generated_id)
        logger.debug(f"[ID_RESOLVER] {table}: Row {row_idx} -> {generated_id}")

    def register_composite_key(self, table: str, key_tuple: Tuple, generated_id: Any):
        """
        Maps a composite key tuple to the generated or reused database primary key.
        """
        if generated_id is None or not any(key_tuple):
            return

        if table not in self._composite_mappings:
            self._composite_mappings[table] = {}
            self._distinct_ids.setdefault(table, set())

        self._composite_mappings[table][key_tuple] = generated_id
        self._distinct_ids[table].add(generated_id)

    def get_id(self, table: str, source_key: Any) -> Optional[Any]:
        """
        Retrieves mapped database ID by source business key.
        Checks exact key, stripped key, normalized key, and integer-string.
        """
        if source_key is None:
            return None

        s_key = str(source_key).strip()
        if not s_key or s_key.lower() in ("nan", "none", "null", "nat"):
            return None

        tbl_map = self._mappings.get(table, {})
        if s_key in tbl_map:
            return tbl_map[s_key]

        norm = normalize_string(s_key)
        if norm in tbl_map:
            return tbl_map[norm]

        lower = s_key.lower()
        if lower in tbl_map:
            return tbl_map[lower]

        try:
            val_f = float(s_key)
            if val_f.is_integer() and str(int(val_f)) in tbl_map:
                return tbl_map[str(int(val_f))]
        except (ValueError, TypeError):
            pass

        return None

    def get_row_id(self, table: str, row_idx: int) -> Optional[Any]:
        """
        Retrieves database primary key generated or reused for a specific source Excel row index.
        """
        row_map = self._row_mappings.get(table, {})
        return row_map.get(int(row_idx))

    def get_composite_id(self, table: str, key_tuple: Tuple) -> Optional[Any]:
        """
        Retrieves database primary key by composite key tuple.
        """
        comp_map = self._composite_mappings.get(table, {})
        return comp_map.get(key_tuple)

    def has_composite_key(self, table: str, key_tuple: Tuple) -> bool:
        return self.get_composite_id(table, key_tuple) is not None

    def has_id(self, table: str, source_key: Any) -> bool:
        return self.get_id(table, source_key) is not None

    def has_row_id(self, table: str, row_idx: int) -> bool:
        return self.get_row_id(table, row_idx) is not None

    def get_distinct_ids(self, table: str) -> List[Any]:
        """
        Returns list of all distinct primary keys captured for a table.
        """
        return list(self._distinct_ids.get(table, set()))

    def get_resolution_report(self) -> Dict[str, Any]:
        """
        Generates comprehensive ID resolution summary for audit and execution reporting.
        """
        report: Dict[str, Any] = {}
        all_tables = set(self._mappings.keys()) | set(self._row_mappings.keys())
        for t in sorted(all_tables):
            distinct_cnt = len(self._distinct_ids.get(t, set()))
            row_cnt = len(self._row_mappings.get(t, {}))
            key_cnt = len(self._mappings.get(t, {}))
            report[t] = {
                "distinct_database_ids": distinct_cnt,
                "rows_mapped": row_cnt,
                "business_keys_indexed": key_cnt,
                "sample_ids": list(self._distinct_ids.get(t, set()))[:10]
            }
        return report


IdResolver = IdResolverStore


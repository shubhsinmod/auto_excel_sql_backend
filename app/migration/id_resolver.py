from typing import Dict, Any, Optional, List, Tuple, Set
from app.mapping.matcher import normalize_string
from app.core.logging import logger

_MISSING_VALUES = frozenset({"", "nan", "none", "null", "nat"})


def _valid_source_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip()
    return None if key.lower() in _MISSING_VALUES else key


def _key_variants(value: Any) -> Tuple[str, ...]:
    """Return the same exact/normalized/lower/numeric key variants as the old resolver."""
    key = _valid_source_key(value)
    if key is None:
        return ()
    variants = [key]
    normalized = normalize_string(key)
    if normalized and normalized not in variants:
        variants.append(normalized)
    lowered = key.lower()
    if lowered not in variants:
        variants.append(lowered)
    try:
        numeric = float(key)
        if numeric.is_integer():
            integer_key = str(int(numeric))
            if integer_key not in variants:
                variants.append(integer_key)
    except (ValueError, TypeError, OverflowError):
        pass
    return tuple(variants)


class IdResolverStore:
    """Migration-scoped ID resolver; key matching semantics remain unchanged."""

    def __init__(self):
        self._mappings: Dict[str, Dict[str, Any]] = {}
        self._row_mappings: Dict[str, Dict[int, Any]] = {}
        self._composite_mappings: Dict[str, Dict[Tuple, Any]] = {}
        self._distinct_ids: Dict[str, Set[Any]] = {}
        # Cache variants only, never resolved IDs, so later registrations remain visible.
        self._variants_cache: Dict[str, Tuple[str, ...]] = {}

    @staticmethod
    def _canonical_table_keys(table: str) -> List[str]:
        raw = str(table or "").strip()
        norm = raw.lower().replace("dbo.", "").replace("_", "").replace(" ", "").strip()
        keys = [raw]
        if raw.startswith("dbo."):
            without_dbo = raw[4:]
            if without_dbo not in keys:
                keys.append(without_dbo)
        else:
            with_dbo = f"dbo.{raw}"
            if with_dbo not in keys:
                keys.append(with_dbo)
        if norm not in keys:
            keys.append(norm)
        return keys

    def _variants(self, value: Any) -> Tuple[str, ...]:
        key = _valid_source_key(value)
        if key is None:
            return ()
        cached = self._variants_cache.get(key)
        if cached is not None:
            return cached
        variants = _key_variants(key)
        self._variants_cache[key] = variants
        return variants

    def register_id(self, table: str, source_key: Any, generated_id: Any):
        if source_key is None or generated_id is None:
            return
        variants = self._variants(source_key)
        if not variants:
            return
        for t_k in self._canonical_table_keys(table):
            table_map = self._mappings.setdefault(t_k, {})
            for variant in variants:
                table_map[variant] = generated_id
            self._distinct_ids.setdefault(t_k, set()).add(generated_id)
        logger.debug("[ID_RESOLVER] %s: '%s' -> %s", table, variants[0], generated_id)

    def register_row_id(self, table: str, row_idx: int, generated_id: Any):
        if generated_id is None:
            return
        r_int = int(row_idx)
        for t_k in self._canonical_table_keys(table):
            self._row_mappings.setdefault(t_k, {})[r_int] = generated_id
            self._distinct_ids.setdefault(t_k, set()).add(generated_id)

    def register_composite_key(self, table: str, key_tuple: Tuple, generated_id: Any):
        if generated_id is None or not any(key_tuple):
            return
        for t_k in self._canonical_table_keys(table):
            self._composite_mappings.setdefault(t_k, {})[key_tuple] = generated_id
            self._distinct_ids.setdefault(t_k, set()).add(generated_id)

    def get_id(self, table: str, source_key: Any) -> Optional[Any]:
        variants = self._variants(source_key)
        if not variants:
            return None
        for t_k in self._canonical_table_keys(table):
            table_map = self._mappings.get(t_k)
            if table_map:
                for variant in variants:
                    result = table_map.get(variant)
                    if result is not None:
                        return result
        return None

    def get_row_id(self, table: str, row_idx: int) -> Optional[Any]:
        r_int = int(row_idx)
        for t_k in self._canonical_table_keys(table):
            row_map = self._row_mappings.get(t_k)
            if row_map and r_int in row_map:
                return row_map[r_int]
        return None

    def get_composite_id(self, table: str, key_tuple: Tuple) -> Optional[Any]:
        for t_k in self._canonical_table_keys(table):
            comp_map = self._composite_mappings.get(t_k)
            if comp_map and key_tuple in comp_map:
                return comp_map[key_tuple]
        return None

    def has_composite_key(self, table: str, key_tuple: Tuple) -> bool:
        return self.get_composite_id(table, key_tuple) is not None

    def has_id(self, table: str, source_key: Any) -> bool:
        return self.get_id(table, source_key) is not None

    def has_row_id(self, table: str, row_idx: int) -> bool:
        return self.get_row_id(table, row_idx) is not None

    def get_distinct_ids(self, table: str) -> List[Any]:
        return list(self._distinct_ids.get(table, set()))

    def get_resolution_report(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {}
        for table in sorted(set(self._mappings) | set(self._row_mappings)):
            ids = self._distinct_ids.get(table, set())
            report[table] = {
                "distinct_database_ids": len(ids),
                "rows_mapped": len(self._row_mappings.get(table, {})),
                "business_keys_indexed": len(self._mappings.get(table, {})),
                "sample_ids": list(ids)[:10],
            }
        return report


IdResolver = IdResolverStore

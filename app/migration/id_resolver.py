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
        table_map = self._mappings.setdefault(table, {})
        for variant in variants:
            table_map[variant] = generated_id
        self._distinct_ids.setdefault(table, set()).add(generated_id)
        logger.debug("[ID_RESOLVER] %s: '%s' -> %s", table, variants[0], generated_id)

    def register_row_id(self, table: str, row_idx: int, generated_id: Any):
        if generated_id is None:
            return
        self._row_mappings.setdefault(table, {})[int(row_idx)] = generated_id
        self._distinct_ids.setdefault(table, set()).add(generated_id)

    def register_composite_key(self, table: str, key_tuple: Tuple, generated_id: Any):
        if generated_id is None or not any(key_tuple):
            return
        self._composite_mappings.setdefault(table, {})[key_tuple] = generated_id
        self._distinct_ids.setdefault(table, set()).add(generated_id)

    def get_id(self, table: str, source_key: Any) -> Optional[Any]:
        variants = self._variants(source_key)
        table_map = self._mappings.get(table)
        if not variants or not table_map:
            return None
        for variant in variants:
            result = table_map.get(variant)
            if result is not None:
                return result
        return None

    def get_row_id(self, table: str, row_idx: int) -> Optional[Any]:
        return self._row_mappings.get(table, {}).get(int(row_idx))

    def get_composite_id(self, table: str, key_tuple: Tuple) -> Optional[Any]:
        return self._composite_mappings.get(table, {}).get(key_tuple)

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

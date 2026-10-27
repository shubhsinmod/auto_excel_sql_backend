import re
import difflib
import unicodedata
from typing import List, Dict, Any, Optional, Tuple
from app.utils.normalization import normalize_cell_value, normalize_string
from app.models.migration import ResolvedEntityRecord
from app.core.logging import logger

# Recognized localities, branch indicators, and sectors in Indian cities / restaurant chains
# Patterns handle names like "BTM", "Hoodi", "Koramangala", "Indiranagar", "Sector 1", "Phase 2", etc.
KNOWN_LOCALITY_PATTERNS = [
    r"\bbtm(?:\s+layout)?\b",
    r"\bhoodi\b",
    r"\bkoramangala\b",
    r"\bindiranagar\b",
    r"\bjayanagar\b",
    r"\bwhitefield\b",
    r"\belectronic\s+city\b",
    r"\bhsr(?:\s+layout)?\b",
    r"\bmarathahalli\b",
    r"\bmalleswaram\b",
    r"\bbellandur\b",
    r"\bhebbal\b",
    r"\bbgp\b",
    r"\bkalyan\s+nagar\b",
    r"\bbannerghatta(?:\s+road)?\b",
    r"\brajagajinagar\b",
    r"\byelahanka\b",
    r"\brr\s+nagar\b",
    r"\bjp\s+nagar\b",
    r"\bsarjapurt?(?:\s+road)?\b",
    r"\bsector\s*\d+\b",
    r"\bphase\s*\d+\b",
    r"\bblock\s*\d+\b",
    r"\bstage\s*\d+\b",
    r"\bmain\s+road\b",
    r"\bcentral\b",
    r"\bairport\b",
    r"\bmetro(?:\s+station)?\b",
]

LOCALITY_REGEX = re.compile(
    r"(?:" + "|".join(KNOWN_LOCALITY_PATTERNS) + r")$",
    re.IGNORECASE
)

# Structural delimiters where branch/locality commonly appears at end
# e.g., "Namaste - BTM", "Namaste (Hoodi)", "Namaste / Whitefield"
DELIMITER_REGEX = re.compile(r"[\s\-_/–—\(\)]+")


def extract_outlet_base_name(name: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Separates a raw outlet string into a base outlet name and an optional branch/locality suffix.
    Guarantees:
    - Never strips brand descriptors like 'Cafe', 'Dhaba', 'Bistro', 'Kitchen', 'Express'.
    - Only strips verified locality patterns or parenthesized/dash-separated localities.
    - If stripped result is empty, returns original string as base name.
    
    Returns:
        (normalized_base_name, detected_locality_or_branch)
    """
    if not name or str(name).strip().lower() in ("nan", "none", "null", "nat"):
        return "", None

    raw = str(name).strip()

    # 1. Check parenthesized suffix e.g. "Namaste (BTM)" or "Namaste (Hoodi Branch)"
    paren_match = re.search(r"^(.*?)\s*\((.*?)\)\s*$", raw)
    if paren_match:
        base_cand = paren_match.group(1).strip()
        loc_cand = paren_match.group(2).strip()
        if base_cand:
            return base_cand, loc_cand

    # 2. Check dash / hyphen / slash delimiter e.g. "Namaste - BTM", "Namaste / Hoodi"
    dash_match = re.search(r"^(.*?)\s*[-–—/]\s*([A-Za-z0-9\s]+)$", raw)
    if dash_match:
        base_cand = dash_match.group(1).strip()
        loc_cand = dash_match.group(2).strip()
        # Verify loc_cand is a known locality or short code/branch
        if base_cand and (LOCALITY_REGEX.search(loc_cand.lower()) or len(loc_cand.split()) <= 2):
            return base_cand, loc_cand

    # 3. Check known locality suffix at end of space-separated string e.g. "Namaste BTM", "Namaste Hoodi"
    loc_match = LOCALITY_REGEX.search(raw.strip())
    if loc_match:
        loc_val = loc_match.group(0).strip()
        base_val = raw[:loc_match.start()].strip()
        # Clean trailing separators like '-' or ','
        base_val = re.sub(r"[\s\-_/–—,]+$", "", base_val).strip()
        if base_val:
            return base_val, loc_val

    # 4. No locality suffix stripped -> return entire string
    return raw, None


def outlet_row_source_values(row_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Extract the two supported outlet identity fields without relying on header casing."""
    restaurant_value = None
    virtual_brand_value = None
    for key, value in row_data.items():
        normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
        clean_value = str(value).strip() if value is not None else ""
        if not clean_value or clean_value.lower() in ("nan", "none", "null", "nat"):
            continue
        if normalized_key in ("restaurantname", "restaurant", "outletname"):
            restaurant_value = clean_value
        # virtual_brand_name is intentionally ignored: the outlet is identified by
        # restaurant_name only (business decision). The second value is kept in the
        # return signature for compatibility and is always None.
    return restaurant_value, virtual_brand_value


def compact_outlet_key(value: Any) -> str:
    """Lowercase letters/digits only: 'Namaste - Vijayanagar' == 'NAMASTE VIJAYANAGAR' == 'Namaste-Vijayanagar'."""
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", str(value)).casefold())


def closest_outlet_names(source: str, db_records: List[Dict[str, Any]], name_col: str = "Outlet_Name", limit: int = 3) -> List[str]:
    """Nearest database outlet names (for error messages only - never used to pick an ID)."""
    names = [str(r.get(name_col) or "").strip() for r in db_records if str(r.get(name_col) or "").strip()]
    key = compact_outlet_key(source)
    by_key = {compact_outlet_key(n): n for n in names}
    hits = difflib.get_close_matches(key, list(by_key.keys()), n=limit, cutoff=0.5)
    return [by_key[h] for h in hits]


def _unique_spelling_variant(branch: Optional[str], candidates: List["OutletMatchCandidate"]) -> Optional["OutletMatchCandidate"]:
    """
    Branch names are spelled differently in POS exports and the database
    ('Vijayanagar' vs 'Vijay Nagar', 'Malleshwaram' vs 'Maleshwaram').
    Accept a candidate only when it is clearly the same word:
      - compact names (letters/digits only) of at least 5 characters,
      - similarity >= 0.90 (about one letter different),
      - exactly one candidate passes, and no other outlet is within 0.05 of it.
    Otherwise return None so the row is reported instead of guessed.
    """
    key = compact_outlet_key(branch)
    if len(key) < 5:
        return None
    scored = []
    for c in candidates:
        if c.id is None:
            continue
        best = 0.0
        for name in (c.compact_name, compact_outlet_key(c.short_code), compact_outlet_key(c.locality)):
            if len(name) >= 5:
                best = max(best, difflib.SequenceMatcher(None, key, name).ratio())
        scored.append((best, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < 0.90:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05:
        return None
    return scored[0][1]


def outlet_row_source_key(row_data: Dict[str, Any]) -> Tuple[str, str]:
    """Return one stable cache key for both outlet-identifying source values."""
    restaurant_value, virtual_brand_value = outlet_row_source_values(row_data)
    return (
        normalize_cell_value(restaurant_value or ""),
        normalize_cell_value(virtual_brand_value or "")
    )


class OutletMatchCandidate:
    def __init__(self, record: Dict[str, Any], id_col: str = "Outlet_ID", name_col: str = "Outlet_Name"):
        self.record = record
        self.id = record.get(id_col)
        self.raw_name = str(record.get(name_col) or "").strip()
        self.norm_name = normalize_cell_value(self.raw_name)
        self.compact_name = compact_outlet_key(self.raw_name)
        self.base_name, self.locality = extract_outlet_base_name(self.raw_name)
        self.norm_base = normalize_cell_value(self.base_name)
        self.address = str(record.get("Address") or "").strip()
        self.short_code = str(record.get("Short_Code") or "").strip()


class OutletMatcher:
    """
    Intelligent Outlet Name Matching Service.
    Implements multi-stage normalized outlet name matching:
    1. Exact match (case-insensitive) against db records.
    2. Branch/Locality match if db has exact branch record.
    3. Normalized base outlet name match (e.g. 'Namaste BTM' -> base 'Namaste').
    4. Ambiguity detection:
       - Multiple db outlets match same base name -> AMBIGUOUS_MATCH.
       - Discrepancy between restaurant_name and virtual_brand_name -> DIFFERENT_OUTLETS_DETECTED.
    5. Safe rejection: Brand descriptors (e.g. 'Namaste Cafe' vs 'Namaste') are not conflated.
    """

    @classmethod
    def match_outlet_name(
        cls,
        source_name: Optional[str],
        db_records: List[Dict[str, Any]],
        id_col: str = "Outlet_ID",
        name_col: str = "Outlet_Name",
        context_address: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Matches a single outlet name string against existing database outlet records.
        Returns a dict:
        {
            "status": "MATCHED" | "AMBIGUOUS" | "UNRESOLVED" | "MISSING_NAME",
            "outlet_id": Any,
            "matched_outlet_name": Optional[str],
            "source_value": Optional[str],
            "normalized_name": str,
            "normalized_base": str,
            "detected_locality": Optional[str],
            "match_type": str,
            "confidence": float,
            "candidates": List[Dict[str, Any]],
            "message": str
        }
        """
        if not source_name or str(source_name).strip().lower() in ("nan", "none", "null", "nat"):
            return {
                "status": "MISSING_NAME",
                "outlet_id": None,
                "matched_outlet_name": None,
                "source_value": source_name,
                "normalized_name": "",
                "normalized_base": "",
                "detected_locality": None,
                "match_type": "NONE",
                "confidence": 0.0,
                "candidates": [],
                "message": "Source outlet name is missing or empty."
            }

        raw_source = str(source_name).strip()
        norm_source = normalize_cell_value(raw_source)
        base_source, loc_source = extract_outlet_base_name(raw_source)
        norm_base_source = normalize_cell_value(base_source)

        if not db_records:
            return {
                "status": "UNRESOLVED",
                "outlet_id": None,
                "matched_outlet_name": None,
                "source_value": raw_source,
                "normalized_name": norm_source,
                "normalized_base": norm_base_source,
                "detected_locality": loc_source,
                "match_type": "NO_RECORDS",
                "confidence": 0.0,
                "candidates": [],
                "message": "Database Outlets table has no existing records."
            }

        candidates = [OutletMatchCandidate(r, id_col, name_col) for r in db_records]

        # Stage 1: Exact case-insensitive match on full raw name
        compact_source = compact_outlet_key(raw_source)
        exact_matches = [
            c for c in candidates
            if c.raw_name.lower() == raw_source.lower() or c.norm_name == norm_source
            or (compact_source and c.compact_name == compact_source)
        ]
        if len(exact_matches) == 1:
            m = exact_matches[0]
            return {
                "status": "MATCHED",
                "outlet_id": m.id,
                "matched_outlet_name": m.raw_name,
                "source_value": raw_source,
                "normalized_name": norm_source,
                "normalized_base": norm_base_source,
                "detected_locality": loc_source,
                "match_type": "EXACT_MATCH",
                "confidence": 1.0,
                "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                "message": f"Exact match on database outlet '{m.raw_name}' (ID={m.id})."
            }
        elif len(exact_matches) > 1:
            # Check if IDs are distinct
            distinct_ids = set(c.id for c in exact_matches)
            if len(distinct_ids) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "outlet_id": None,
                    "matched_outlet_name": None,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "AMBIGUOUS_MATCH",
                    "confidence": 0.5,
                    "candidates": [{"id": c.id, "name": c.raw_name, "record": c.record} for c in exact_matches],
                    "message": f"Multiple database outlets with identical name '{raw_source}'. User review required."
                }
            else:
                m = exact_matches[0]
                return {
                    "status": "MATCHED",
                    "outlet_id": m.id,
                    "matched_outlet_name": m.raw_name,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "EXACT_MATCH",
                    "confidence": 1.0,
                    "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                    "message": f"Exact match on database outlet '{m.raw_name}' (ID={m.id})."
                }

        # Stage 2: Accept a suffix only when it exactly identifies an existing outlet.
        if loc_source:
            norm_loc = normalize_cell_value(loc_source)
            suffix_matches = [
                c for c in candidates
                if c.id is not None and norm_loc and (
                    normalize_cell_value(c.raw_name) == norm_loc
                    or normalize_cell_value(c.short_code) == norm_loc
                    or (c.locality and normalize_cell_value(c.locality) == norm_loc)
                )
            ]
            if not suffix_matches:
                # Compact compare: 'J P Nagar' == 'JP Nagar', 'Old-Madras Road' == 'OLD madras road'
                compact_loc = compact_outlet_key(loc_source)
                suffix_matches = [
                    c for c in candidates
                    if c.id is not None and compact_loc and compact_loc in (
                        c.compact_name, compact_outlet_key(c.short_code), compact_outlet_key(c.locality)
                    )
                ]
            fuzzy_suffix = False
            if not suffix_matches:
                fuzzy_hit = _unique_spelling_variant(loc_source, candidates)
                if fuzzy_hit is not None:
                    suffix_matches = [fuzzy_hit]
                    fuzzy_suffix = True
            suffix_ids = {str(c.id) for c in suffix_matches}
            if len(suffix_ids) == 1 and fuzzy_suffix:
                m = suffix_matches[0]
                logger.warning(
                    "[OUTLET_SPELLING_VARIANT_MATCH] '%s' -> branch '%s' matched database outlet '%s' (ID=%s)",
                    raw_source, loc_source, m.raw_name, m.id,
                )
                return {
                    "status": "MATCHED",
                    "outlet_id": m.id,
                    "matched_outlet_name": m.raw_name,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "BRANCH_SUFFIX_SPELLING_VARIANT",
                    "confidence": 0.95,
                    "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                    "message": (
                        f"Branch '{loc_source}' matched outlet '{m.raw_name}' (ID={m.id}) as a spelling variant "
                        f"(only one close outlet name)."
                    )
                }
            if len(suffix_ids) == 1:
                m = suffix_matches[0]
                return {
                    "status": "MATCHED",
                    "outlet_id": m.id,
                    "matched_outlet_name": m.raw_name,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "BRANCH_SUFFIX_MATCH",
                    "confidence": 0.98,
                    "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                    "message": f"Branch suffix '{loc_source}' uniquely identifies outlet '{m.raw_name}' (ID={m.id})."
                }
            if len(suffix_ids) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "outlet_id": None,
                    "matched_outlet_name": None,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "AMBIGUOUS_MATCH",
                    "confidence": 0.5,
                    "candidates": [{"id": c.id, "name": c.raw_name, "record": c.record} for c in suffix_matches],
                    "message": f"Branch suffix '{loc_source}' identifies multiple existing outlets. User review required."
                }

            branch_matches = [
                c for c in candidates
                if c.norm_base == norm_base_source and (
                    norm_loc in c.norm_name
                    or (c.locality and normalize_cell_value(c.locality) == norm_loc)
                    or (c.address and norm_loc in normalize_cell_value(c.address))
                    or (c.short_code and norm_loc in normalize_cell_value(c.short_code))
                )
            ]
            if len(branch_matches) == 1:
                m = branch_matches[0]
                return {
                    "status": "MATCHED",
                    "outlet_id": m.id,
                    "matched_outlet_name": m.raw_name,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "BRANCH_MATCH",
                    "confidence": 0.98,
                    "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                    "message": f"Branch-specific match on database outlet '{m.raw_name}' (ID={m.id})."
                }
            elif len(branch_matches) > 1:
                distinct_ids = set(c.id for c in branch_matches)
                if len(distinct_ids) > 1:
                    return {
                        "status": "AMBIGUOUS",
                        "outlet_id": None,
                        "matched_outlet_name": None,
                        "source_value": raw_source,
                        "normalized_name": norm_source,
                        "normalized_base": norm_base_source,
                        "detected_locality": loc_source,
                        "match_type": "AMBIGUOUS_MATCH",
                        "confidence": 0.5,
                        "candidates": [{"id": c.id, "name": c.raw_name, "record": c.record} for c in branch_matches],
                        "message": f"Multiple database branch outlets matched '{raw_source}'. User review required."
                    }

        # Stage 3: Normalized base outlet name matching
        # Matches base name against database outlet names
        # E.g., Excel: "Namaste BTM" (base: "Namaste") vs Database: "Namaste"
        norm_loc_source = compact_outlet_key(loc_source) if loc_source else ""
        base_matches = [
            c for c in candidates
            if (c.norm_name == norm_base_source or c.norm_base == norm_base_source)
            # A different branch is never the same outlet: 'Namaste - Vijayanagar'
            # must not resolve to 'Namaste BTM' just because both start with 'Namaste'.
            and not (norm_loc_source and c.locality and compact_outlet_key(c.locality) != norm_loc_source)
        ]

        if len(base_matches) == 1:
            m = base_matches[0]
            # Safety check: Ensure this is a genuine base match and not conflating brand descriptors
            # E.g. "Namaste Cafe" has norm_base_source "namaste cafe", which won't equal "namaste"
            # But if db is "Namaste" and excel is "Namaste Cafe", base_source is "Namaste Cafe", no match.
            return {
                "status": "MATCHED",
                "outlet_id": m.id,
                "matched_outlet_name": m.raw_name,
                "source_value": raw_source,
                "normalized_name": norm_source,
                "normalized_base": norm_base_source,
                "detected_locality": loc_source,
                "match_type": "NORMALIZED_BASE_MATCH",
                "confidence": 0.95,
                "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                "message": f"Normalized base match: '{raw_source}' -> '{m.raw_name}' (ID={m.id})."
            }
        elif len(base_matches) > 1:
            # If multiple database outlets share the base name (e.g. db has separate "Namaste BTM" (ID=1) and "Namaste Hoodi" (ID=2))
            # and source Excel is generic "Namaste" or has a locality that wasn't uniquely resolved:
            distinct_ids = set(c.id for c in base_matches)
            if len(distinct_ids) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "outlet_id": None,
                    "matched_outlet_name": None,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "AMBIGUOUS_MATCH",
                    "confidence": 0.5,
                    "candidates": [{"id": c.id, "name": c.raw_name, "record": c.record} for c in base_matches],
                    "message": (
                        f"Ambiguous outlet match: Multiple distinct database outlets ({len(base_matches)}) "
                        f"found for base name '{base_source}'. User review required."
                    )
                }
            else:
                m = base_matches[0]
                return {
                    "status": "MATCHED",
                    "outlet_id": m.id,
                    "matched_outlet_name": m.raw_name,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "NORMALIZED_BASE_MATCH",
                    "confidence": 0.95,
                    "candidates": [{"id": m.id, "name": m.raw_name, "record": m.record}],
                    "message": f"Normalized base match: '{raw_source}' -> '{m.raw_name}' (ID={m.id})."
                }

        # Stage 3b: Substring / partial matches check for ambiguity
        partial_matches = [
            c for c in candidates
            if norm_source and (norm_source in c.norm_name or c.norm_name in norm_source)
        ]
        if len(partial_matches) > 1:
            distinct_ids = set(c.id for c in partial_matches)
            if len(distinct_ids) > 1:
                return {
                    "status": "AMBIGUOUS",
                    "outlet_id": None,
                    "matched_outlet_name": None,
                    "source_value": raw_source,
                    "normalized_name": norm_source,
                    "normalized_base": norm_base_source,
                    "detected_locality": loc_source,
                    "match_type": "AMBIGUOUS_MATCH",
                    "confidence": 0.5,
                    "candidates": [{"id": c.id, "name": c.raw_name, "record": c.record} for c in partial_matches],
                    "message": f"Ambiguous: Name '{raw_source}' partially matched {len(partial_matches)} database outlets. User review required."
                }

        # Stage 4: No match found
        return {
            "status": "UNRESOLVED",
            "outlet_id": None,
            "matched_outlet_name": None,
            "source_value": raw_source,
            "normalized_name": norm_source,
            "normalized_base": norm_base_source,
            "detected_locality": loc_source,
            "match_type": "NO_MATCH",
            "confidence": 0.0,
            "candidates": [],
            "message": (
                f"No existing database outlet found matching '{raw_source}' "
                f"(compared case/space/punctuation-insensitively as '{compact_outlet_key(raw_source)}')."
                + (
                    f" Closest outlet names in database: {', '.join(repr(n) for n in closest_outlet_names(raw_source, db_records, name_col))}."
                    if closest_outlet_names(raw_source, db_records, name_col) else
                    f" Database has {len(db_records)} outlet(s); none is similar."
                )
            )
        }

    @classmethod
    def resolve_row_outlet(
        cls,
        row_data: Dict[str, Any],
        db_records: List[Dict[str, Any]],
        id_col: str = "Outlet_ID",
        name_col: str = "Outlet_Name",
        restaurant_col: Optional[str] = None,
        virtual_brand_col: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Evaluates both 'restaurant_name' and 'virtual_brand_name' independently:
        - If only one column contains a value, use that value for outlet matching.
        - If both contain values:
          - If both resolve to the same database outlet ID -> MATCHED.
          - If they resolve to different database outlet IDs -> DIFFERENT_OUTLETS_DETECTED (Flagged for review).
        - Preserves original source values for traceability.
        """
        if restaurant_col:
            r_val = row_data.get(restaurant_col)
            r_clean = str(r_val).strip() if r_val is not None else None
            if r_clean and r_clean.lower() in ("nan", "none", "null", "nat"):
                r_clean = None
        else:
            r_clean, _ = outlet_row_source_values(row_data)
        # Outlet is identified by restaurant_name only; virtual_brand_name is ignored.
        v_clean = None

        # Case A: Neither has value
        if not r_clean and not v_clean:
            return {
                "status": "MISSING_NAME",
                "outlet_id": None,
                "matched_outlet_name": None,
                "restaurant_value": None,
                "virtual_brand_value": None,
                "source_value": None,
                "match_type": "NONE",
                "confidence": 0.0,
                "candidates": [],
                "message": "Neither restaurant_name nor virtual_brand_name provided in row."
            }

        # Case B: Only restaurant_name has value
        if r_clean and not v_clean:
            res = cls.match_outlet_name(r_clean, db_records, id_col, name_col)
            res["restaurant_value"] = r_clean
            res["virtual_brand_value"] = None
            return res

        # Case C: Only virtual_brand_name has value
        if v_clean and not r_clean:
            res = cls.match_outlet_name(v_clean, db_records, id_col, name_col)
            res["restaurant_value"] = None
            res["virtual_brand_value"] = v_clean
            return res

        # Case D: Both have values -> Evaluate independently
        res_r = cls.match_outlet_name(r_clean, db_records, id_col, name_col)
        res_v = cls.match_outlet_name(v_clean, db_records, id_col, name_col)

        # Subcase D1: Both matched to the same database outlet ID
        if res_r["status"] == "MATCHED" and res_v["status"] == "MATCHED":
            if res_r["outlet_id"] == res_v["outlet_id"]:
                return {
                    "status": "MATCHED",
                    "outlet_id": res_r["outlet_id"],
                    "matched_outlet_name": res_r["matched_outlet_name"],
                    "restaurant_value": r_clean,
                    "virtual_brand_value": v_clean,
                    "source_value": f"restaurant_name='{r_clean}', virtual_brand_name='{v_clean}'",
                    "normalized_name": res_r["normalized_name"],
                    "normalized_base": res_r["normalized_base"],
                    "detected_locality": res_r["detected_locality"],
                    "match_type": f"DUAL_COLUMN_MATCH ({res_r['match_type']})",
                    "confidence": min(res_r["confidence"], res_v["confidence"]),
                    "candidates": res_r["candidates"],
                    "message": (
                        f"Both 'restaurant_name' ('{r_clean}') and 'virtual_brand_name' ('{v_clean}') "
                        f"consistently resolved to outlet '{res_r['matched_outlet_name']}' (ID={res_r['outlet_id']})."
                    )
                }
            else:
                # Discrepancy between the two columns! Flag for user review.
                logger.warning(
                    f"[OUTLET_MATCHER] Discrepancy: restaurant_name '{r_clean}' resolved to ID={res_r['outlet_id']}, "
                    f"virtual_brand_name '{v_clean}' resolved to ID={res_v['outlet_id']}."
                )
                return {
                    "status": "CONFLICTING_OUTLET_VALUES",
                    "outlet_id": None,
                    "matched_outlet_name": None,
                    "restaurant_value": r_clean,
                    "virtual_brand_value": v_clean,
                    "source_value": f"restaurant_name='{r_clean}', virtual_brand_name='{v_clean}'",
                    "normalized_name": f"{res_r['normalized_name']} / {res_v['normalized_name']}",
                    "normalized_base": f"{res_r['normalized_base']} / {res_v['normalized_base']}",
                    "detected_locality": res_r["detected_locality"] or res_v["detected_locality"],
                    "match_type": "CONFLICTING_OUTLET_VALUES",
                    "confidence": 0.4,
                    "candidates": res_r["candidates"] + res_v["candidates"],
                    "message": (
                        f"Discrepancy: 'restaurant_name' ('{r_clean}') resolves to Outlet ID {res_r['outlet_id']} ('{res_r['matched_outlet_name']}'), "
                        f"while 'virtual_brand_name' ('{v_clean}') resolves to Outlet ID {res_v['outlet_id']} ('{res_v['matched_outlet_name']}'). "
                        f"User review required."
                    )
                }

        # Subcase D2: One matched, other unresolved
        if res_r["status"] == "MATCHED" and res_v["status"] == "UNRESOLVED":
            res_r["restaurant_value"] = r_clean
            res_r["virtual_brand_value"] = v_clean
            res_r["message"] += f" (virtual_brand_name '{v_clean}' was unresolved)."
            return res_r

        if res_v["status"] == "MATCHED" and res_r["status"] == "UNRESOLVED":
            res_v["restaurant_value"] = r_clean
            res_v["virtual_brand_value"] = v_clean
            res_v["message"] += f" (restaurant_name '{r_clean}' was unresolved)."
            return res_v

        # Subcase D3: At least one is ambiguous
        if res_r["status"] == "AMBIGUOUS" or res_v["status"] == "AMBIGUOUS":
            cand_pool = res_r["candidates"] + res_v["candidates"]
            return {
                "status": "AMBIGUOUS",
                "outlet_id": None,
                "matched_outlet_name": None,
                "restaurant_value": r_clean,
                "virtual_brand_value": v_clean,
                "source_value": f"restaurant_name='{r_clean}', virtual_brand_name='{v_clean}'",
                "normalized_name": f"{res_r['normalized_name']} / {res_v['normalized_name']}",
                "normalized_base": f"{res_r['normalized_base']} / {res_v['normalized_base']}",
                "detected_locality": res_r["detected_locality"] or res_v["detected_locality"],
                "match_type": "AMBIGUOUS_MATCH",
                "confidence": 0.5,
                "candidates": cand_pool,
                "message": f"Ambiguous outlet match between restaurant_name ('{r_clean}') and virtual_brand_name ('{v_clean}'). User review required."
            }

        # Subcase D4: Both unresolved
        return {
            "status": "UNRESOLVED",
            "outlet_id": None,
            "matched_outlet_name": None,
            "restaurant_value": r_clean,
            "virtual_brand_value": v_clean,
            "source_value": f"restaurant_name='{r_clean}', virtual_brand_name='{v_clean}'",
            "normalized_name": f"{res_r['normalized_name']} / {res_v['normalized_name']}",
            "normalized_base": f"{res_r['normalized_base']} / {res_v['normalized_base']}",
            "detected_locality": res_r["detected_locality"] or res_v["detected_locality"],
            "match_type": "NO_MATCH",
            "confidence": 0.0,
            "candidates": [],
            "message": f"Neither restaurant_name ('{r_clean}') nor virtual_brand_name ('{v_clean}') matched any database outlet."
        }


class OutletCache:
    """
    High-performance in-memory cache of database outlets preloaded from dbo.Outlets.
    Supports case-insensitive, whitespace-normalized, and punctuation-normalized lookup.
    Handles single and multiple outlets cleanly.
    """
    def __init__(self, db_records: Optional[List[Dict[str, Any]]] = None):
        self.records = db_records or []
        self.exact_norm_map: Dict[str, Any] = {}
        self.id_to_record: Dict[Any, Dict[str, Any]] = {}
        self._build_index()

    def _build_index(self) -> None:
        for r in self.records:
            o_id = r.get("Outlet_ID")
            o_name = str(r.get("Outlet_Name") or "").strip()
            if o_id is None or not o_name:
                continue
            self.id_to_record[o_id] = r
            
            # Exact normalized name (lowercase, whitespace collapsed)
            norm = normalize_cell_value(o_name)
            if norm:
                self.exact_norm_map[norm] = o_id
                self.exact_norm_map[norm.replace(" ", "")] = o_id
            
            raw_clean = o_name.lower().strip()
            self.exact_norm_map[raw_clean] = o_id
            compact = compact_outlet_key(o_name)
            if compact:
                self.exact_norm_map.setdefault(compact, o_id)
            self.exact_norm_map[raw_clean.replace(" ", "")] = o_id

            # Short code if present
            s_code = str(r.get("Short_Code") or "").strip()
            if s_code and s_code.lower() not in ("none", "null", "nan"):
                self.exact_norm_map[normalize_cell_value(s_code)] = o_id
                self.exact_norm_map[s_code.lower().strip()] = o_id
                self.exact_norm_map[s_code.lower().replace(" ", "")] = o_id

    def find_outlet_id(self, source_name: Optional[Any]) -> Optional[Any]:
        """
        Finds the Outlet_ID from an outlet name using normalized matching
        (case-insensitive, spacing-collapsed, and suffix-tolerant).
        """
        if source_name is None:
            return None
        raw = str(source_name).strip()
        if not raw or raw.lower() in ("nan", "none", "null", "nat"):
            return None
        
        # 1. Exact normalized match (handles case-insensitive and spacing)
        norm = normalize_cell_value(raw)
        if norm in self.exact_norm_map:
            return self.exact_norm_map[norm]
        
        # 2. Match without any spaces
        norm_no_spaces = norm.replace(" ", "")
        if norm_no_spaces in self.exact_norm_map:
            return self.exact_norm_map[norm_no_spaces]

        raw_lower_no_spaces = raw.lower().replace(" ", "").replace("_", "").replace("-", "")
        if raw_lower_no_spaces in self.exact_norm_map:
            return self.exact_norm_map[raw_lower_no_spaces]
        compact = compact_outlet_key(raw)
        if compact and compact in self.exact_norm_map:
            return self.exact_norm_map[compact]

        # 3. Strip common generic branch words ('layout', 'branch', 'outlet', 'store', 'restaurant')
        clean_tokens = [t for t in norm.split() if t not in ('layout', 'branch', 'outlet', 'store', 'restaurant', 'cafe')]
        clean_norm = " ".join(clean_tokens)
        if clean_norm and clean_norm in self.exact_norm_map:
            return self.exact_norm_map[clean_norm]
        if clean_norm and clean_norm.replace(" ", "") in self.exact_norm_map:
            return self.exact_norm_map[clean_norm.replace(" ", "")]

        # 4. Check base name and locality extraction (e.g. "Namaste BTM" -> "BTM")
        base, loc = extract_outlet_base_name(raw)
        if loc:
            norm_loc = normalize_cell_value(loc)
            if norm_loc in self.exact_norm_map:
                return self.exact_norm_map[norm_loc]
            clean_loc = " ".join([t for t in norm_loc.split() if t not in ('layout', 'branch', 'outlet', 'store')])
            if clean_loc in self.exact_norm_map:
                return self.exact_norm_map[clean_loc]

        if base:
            norm_base = normalize_cell_value(base)
            if norm_base in self.exact_norm_map:
                return self.exact_norm_map[norm_base]
            clean_base = " ".join([t for t in norm_base.split() if t not in ('layout', 'branch', 'outlet', 'store')])
            if clean_base in self.exact_norm_map:
                return self.exact_norm_map[clean_base]

        # 5. Fallback to OutletMatcher.match_outlet_name
        res = OutletMatcher.match_outlet_name(raw, self.records)
        if res.get("status") == "MATCHED":
            return res.get("outlet_id")

        return None

    def resolve_row_outlet_id(self, row_data: Dict[str, Any]) -> Optional[Any]:
        """
        Resolves Outlet_ID from row data containing restaurant_name, virtual_brand_name,
        or outlet_name.
        """
        if not isinstance(row_data, dict):
            return None
        r_clean, v_clean = outlet_row_source_values(row_data)
        
        # Check restaurant_name first
        if r_clean:
            oid = self.find_outlet_id(r_clean)
            if oid is not None:
                return oid
                

        # Check any column with 'outlet' in its name
        for k, v in row_data.items():
            if "outlet" in str(k).lower() and v is not None:
                oid = self.find_outlet_id(v)
                if oid is not None:
                    return oid

        return None

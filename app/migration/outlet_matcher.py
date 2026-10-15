import re
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
        elif normalized_key in ("virtualbrandname", "virtualbrand", "brandname"):
            virtual_brand_value = clean_value
    return restaurant_value, virtual_brand_value


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
        exact_matches = [
            c for c in candidates
            if c.raw_name.lower() == raw_source.lower() or c.norm_name == norm_source
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
            suffix_ids = {str(c.id) for c in suffix_matches}
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
        base_matches = [
            c for c in candidates
            if c.norm_name == norm_base_source or c.norm_base == norm_base_source
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
            "message": f"No existing database outlet found matching '{raw_source}'."
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
        if restaurant_col and virtual_brand_col:
            r_val = row_data.get(restaurant_col)
            v_val = row_data.get(virtual_brand_col)
            r_clean = str(r_val).strip() if r_val is not None else None
            v_clean = str(v_val).strip() if v_val is not None else None
        else:
            r_clean, v_clean = outlet_row_source_values(row_data)

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

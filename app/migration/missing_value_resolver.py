import re
from typing import Dict, List, Any, Optional, Set, Tuple
import pandas as pd
from sqlalchemy import text, Engine
from app.models.schema import TableMetadata, ColumnMetadata
from app.utils.normalization import normalize_string, normalize_cell_value
from app.core.logging import logger


def is_empty_or_na(val: Any) -> bool:
    """Checks if a cell value is null, NaN, NaT, or empty/whitespace."""
    if val is None:
        return True
    if isinstance(val, float) and (pd.isna(val)):
        return True
    if pd.isna(val):
        return True
    s = str(val).strip()
    return s == "" or s.lower() in ("nan", "none", "null", "nat")


def extract_sql_default_value(raw_default: Optional[str]) -> Optional[str]:
    """
    Cleans SQL Server default constraints like ('Active') or ((0)) into clean literal strings.
    Handles SQL Server expressions such as ((0)), ('N/A'), (getdate()), ('Pending').
    """
    if raw_default is None:
        return None
    s = str(raw_default).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None

    # Iteratively strip enclosing parentheses (common in SQL Server definitions e.g. ((0)), ('Active'))
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()

    # Strip single or double quotes for literal string defaults
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]

    # Handle standard SQL Server function defaults
    s_upper = s.upper()
    if s_upper in ("GETDATE()", "CURRENT_TIMESTAMP", "SYSDATETIME()"):
        return "CURRENT_TIMESTAMP"
    if s_upper in ("NEWID()", "NEWSEQUENTIALID()"):
        return "NEWID()"

    return s


IDENTIFYING_COLUMN_KEYWORDS = (
    "phone", "contact", "mobile", "cell", "email", "code", "barcode",
    "card", "account", "tax", "pan", "ssn", "gstin", "id", "ref", "username"
)


def identify_table_lookup_columns(table_meta: TableMetadata) -> List[str]:
    """
    Identifies candidate unique/identifying columns for a table in priority order:
    1. Primary key columns (non-identity or identity)
    2. Columns with unique constraint
    3. Natural business keys (phone, contact_number, code, email, id, etc.)
    """
    pks = [c.name for c in table_meta.columns if c.primary_key]
    uniques = [c.name for c in table_meta.columns if c.unique and c.name not in pks]
    business_keys = []
    for c in table_meta.columns:
        if c.name in pks or c.name in uniques:
            continue
        c_lower = c.name.lower()
        if any(k in c_lower for k in IDENTIFYING_COLUMN_KEYWORDS):
            business_keys.append(c.name)
    return pks + uniques + business_keys


class TableEntityLookupIndex:
    """
    High-performance O(1) hash index for existing entity records in a database table.
    Enables retrieving missing required column values using any reliable unique key
    or identifying attribute (e.g. phone, code, email, id, card_no).
    """
    def __init__(self, db_records: List[Dict[str, Any]], identifying_columns: List[str]):
        self.identifying_columns = [c.lower() for c in identifying_columns]
        # (col_name_lower, raw_str_val) -> db_record
        self.exact_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # (col_name_lower, norm_str_val) -> db_record
        self.norm_index: Dict[Tuple[str, str], Dict[str, Any]] = {}

        self.exact_counts: Dict[Tuple[str, str], int] = {}
        self.norm_counts: Dict[Tuple[str, str], int] = {}

        for r in db_records:
            for c_raw in identifying_columns:
                c_lower = c_raw.lower()
                # Find matching key in r (case-insensitive)
                r_val = r.get(c_raw)
                if r_val is None:
                    for k, v in r.items():
                        if k.lower() == c_lower:
                            r_val = v
                            break

                if r_val is not None:
                    s = str(r_val).strip()
                    if s and s.lower() not in ("nan", "none", "null", "nat"):
                        key_exact = (c_lower, s)
                        key_norm = (c_lower, normalize_string(s))
                        self.exact_counts[key_exact] = self.exact_counts.get(key_exact, 0) + 1
                        self.norm_counts[key_norm] = self.norm_counts.get(key_norm, 0) + 1
                        self.exact_index[key_exact] = r
                        self.norm_index[key_norm] = r

    def is_ambiguous(self, col_name: str, val: Any) -> bool:
        """Checks if a lookup value matches multiple distinct records in the table."""
        if val is None or is_empty_or_na(val):
            return False
        s = str(val).strip()
        c_lower = col_name.lower()
        if self.exact_counts.get((c_lower, s), 0) > 1:
            return True
        norm_s = normalize_string(s)
        if self.norm_counts.get((c_lower, norm_s), 0) > 1:
            return True
        return False

    def find_record(self, col_name: str, val: Any) -> Optional[Dict[str, Any]]:
        """Look up a record in O(1) time using column name and value."""
        if val is None or is_empty_or_na(val):
            return None
        s = str(val).strip()
        c_lower = col_name.lower()
        rec = self.exact_index.get((c_lower, s))
        if rec is not None:
            return rec
        return self.norm_index.get((c_lower, normalize_string(s)))


class MissingValueResolution:
    """Represents the outcome of attempting to resolve a missing field value."""
    def __init__(
        self,
        status: str,  # "RESOLVED", "DEFAULT", "NULLABLE", "UNRESOLVED"
        value: Optional[Any],
        value_source: str,  # "DATABASE_LOOKUP", "DATABASE_DEFAULT", "NULL", "UNRESOLVED", "BUSINESS_RULE"
        lookup_column: Optional[str] = None,
        lookup_value: Optional[str] = None,
        message: str = ""
    ):
        self.status = status
        self.value = value
        self.value_source = value_source
        self.lookup_column = lookup_column
        self.lookup_value = lookup_value
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "value_source": self.value_source,
            "lookup_column": self.lookup_column,
            "lookup_value": self.lookup_value,
            "message": self.message,
        }

    def __repr__(self):
        return f"<MissingValueResolution status={self.status} src={self.value_source} val={self.value}>"


def is_customer_name_column(table_name: str, column_name: str) -> bool:
    """Checks whether table and column target dbo.Customer.Customer_Name specifically."""
    clean_tbl = str(table_name).strip().replace("[", "").replace("]", "").lower().split(".")[-1]
    clean_col = str(column_name).strip().replace("[", "").replace("]", "").lower()
    return clean_tbl == "customer" and clean_col == "customer_name"


class MissingValueResolver:
    """
    Automatic Missing Value Resolution Engine.
    When a required Excel field is empty (e.g. Customer_Name is NOT NULL in SQL Server):
    1. First attempts to retrieve its value from existing database records using a reliable unique key.
    2. If found, populates it dynamically from the database (ValueSource = DATABASE_LOOKUP).
    3. If a valid database default or approved deterministic rule exists, uses it where appropriate.
    4. Never inserts NULL into NOT NULL columns.
    5. Never invents customer names, IDs, or placeholder values without an approved business rule.
    6. If the required value cannot be resolved, marks the row UNRESOLVED with clear explanation.
    """

    _index_cache: Dict[str, TableEntityLookupIndex] = {}

    @classmethod
    def clear_cache(cls):
        cls._index_cache.clear()

    @classmethod
    def load_table_entity_index(
        cls,
        engine: Engine,
        table_meta: TableMetadata,
        lookup_columns: Optional[List[str]] = None,
        additional_columns: Optional[List[str]] = None
    ) -> TableEntityLookupIndex:
        """
        Queries SQL Server to build an O(1) hash index of existing records for dynamic lookup.
        """
        table_name = table_meta.name
        if table_name in cls._index_cache:
            return cls._index_cache[table_name]

        identifying_cols = lookup_columns or identify_table_lookup_columns(table_meta)
        req_cols = [c.name for c in table_meta.columns if not c.nullable and not getattr(c, "identity", False)]
        all_cols_to_fetch = list(set(identifying_cols + req_cols + (additional_columns or [])))

        # Match columns against actual schema columns
        actual_cols = [c.name for c in table_meta.columns if c.name in all_cols_to_fetch]
        if not actual_cols:
            actual_cols = [c.name for c in table_meta.columns[:8]]

        dialect_name = engine.name
        clean_tbl = str(table_name).strip().replace("[", "").replace("]", "")
        quoted_tbl = f"[{clean_tbl}]" if dialect_name == "mssql" else f'"{clean_tbl}"'
        quoted_cols = [f"[{c}]" if dialect_name == "mssql" else f'"{c}"' for c in actual_cols]

        db_records: List[Dict[str, Any]] = []
        try:
            with engine.connect() as conn:
                if dialect_name == "mssql":
                    sql = f"SELECT TOP 20000 {', '.join(quoted_cols)} FROM {quoted_tbl}"
                else:
                    sql = f"SELECT {', '.join(quoted_cols)} FROM {quoted_tbl} LIMIT 20000"
                res = conn.execute(text(sql)).mappings().all()
                db_records = [dict(r) for r in res]
                logger.debug(f"[MISSING_VALUE_RESOLVER] Loaded {len(db_records)} records for {table_name}")
        except Exception as e:
            logger.warning(f"[MISSING_VALUE_RESOLVER] Could not load records for {table_name}: {e}")

        index = TableEntityLookupIndex(db_records, identifying_cols)
        cls._index_cache[table_name] = index
        return index

    @classmethod
    def resolve_missing_value(
        cls,
        table_name: str,
        target_column: str,
        table_meta: TableMetadata,
        source_row_data: Dict[str, Any],
        db_to_excel: Dict[str, str],
        entity_lookup_index: Optional[TableEntityLookupIndex] = None,
        approved_business_rules: Optional[Dict[str, Any]] = None
    ) -> MissingValueResolution:
        """
        Dynamically resolves a missing value for a column in a specific source row.
        Follows strict safety and database constraint rules:
        - Missing text values are represented as empty strings at insert time
        - Nullable column -> permitted as NULL
        - Identity column -> auto-generated by SQL Server
        - Required column -> 1st: dynamic DB lookup using reliable unique key
                             2nd: valid database default constraint
                             3rd: approved business rule
                             4th: UNRESOLVED (blocks NULL insert into NOT NULL column)
        """
        # If dbo.Customer.Customer_Name is missing in Excel, normalize to empty string "" (column is NOT NULL).
        # Do NOT attempt DB lookup for this field.
        if is_customer_name_column(table_name, target_column):
            return MissingValueResolution(
                status="RESOLVED",
                value="",
                value_source="DEFAULT",
                message="Missing Customer_Name replaced with empty string."
            )

        col_meta: Optional[ColumnMetadata] = next(
            (c for c in table_meta.columns if c.name.lower() == target_column.lower()),
            None
        )

        if not col_meta:
            return MissingValueResolution(
                status="UNRESOLVED",
                value=None,
                value_source="UNRESOLVED",
                message=f"Target column '{target_column}' not found in table '{table_name}'."
            )

        # 1. Nullable column: allowed to be empty / NULL
        if col_meta.nullable:
            return MissingValueResolution(
                status="NULLABLE",
                value=None,
                value_source="NULL",
                message=f"Column '{target_column}' in table '{table_name}' is nullable. Permitted as NULL."
            )

        # 2. Identity column: generated by SQL Server
        if getattr(col_meta, "identity", False):
            return MissingValueResolution(
                status="RESOLVED",
                value="Generated by SQL Server",
                value_source="IDENTITY",
                message=f"Column '{target_column}' is an identity column generated by SQL Server."
            )

        # 3. Column is NOT NULL without identity -> Attempt dynamic lookup from existing DB records
        identifying_cols = identify_table_lookup_columns(table_meta)
        found_lookup_key: Optional[str] = None
        found_lookup_val: Optional[Any] = None

        # Helper to extract value from source_row_data (checking db_col name and excel_col name)
        def get_source_val(c_name: str) -> Optional[Any]:
            # Direct db_col check
            if c_name in source_row_data and not is_empty_or_na(source_row_data[c_name]):
                return source_row_data[c_name]
            # Via db_to_excel mapping
            ex_col = db_to_excel.get(c_name)
            if ex_col and ex_col in source_row_data and not is_empty_or_na(source_row_data[ex_col]):
                return source_row_data[ex_col]
            # Case-insensitive checks
            c_low = c_name.lower()
            for k, v in source_row_data.items():
                if k.lower() == c_low and not is_empty_or_na(v):
                    return v
            return None

        # Search for available lookup keys in this row (exclude the missing target column itself)
        for key_col in identifying_cols:
            if key_col.lower() == target_column.lower():
                continue
            val = get_source_val(key_col)
            if val is not None and not is_empty_or_na(val):
                found_lookup_key = key_col
                found_lookup_val = val
                break

        fail_reason: str = ""

        if found_lookup_key is not None and found_lookup_val is not None and entity_lookup_index is not None:
            if entity_lookup_index.is_ambiguous(found_lookup_key, found_lookup_val):
                return MissingValueResolution(
                    status="AMBIGUOUS",
                    value=None,
                    value_source="AMBIGUOUS",
                    lookup_column=found_lookup_key,
                    lookup_value=str(found_lookup_val),
                    message=(
                        f"Ambiguous match: Multiple existing records in table '{table_name}' "
                        f"matched {found_lookup_key}='{found_lookup_val}'. User review required."
                    )
                )

            db_rec = entity_lookup_index.find_record(found_lookup_key, found_lookup_val)
            if db_rec is not None:
                # Find target_column value in matched DB record (case-insensitive)
                db_val = db_rec.get(target_column)
                if db_val is None:
                    t_low = target_column.lower()
                    for k, v in db_rec.items():
                        if k.lower() == t_low:
                            db_val = v
                            break

                if db_val is not None and not is_empty_or_na(db_val):
                    # Successfully resolved from existing database record!
                    return MissingValueResolution(
                        status="RESOLVED",
                        value=str(db_val),
                        value_source="DATABASE_LOOKUP",
                        lookup_column=found_lookup_key,
                        lookup_value=str(found_lookup_val),
                        message=(
                            f"Dynamically populated '{target_column}'='{db_val}' from existing record in '{table_name}' "
                            f"using verified unique key {found_lookup_key}='{found_lookup_val}'."
                        )
                    )
                else:
                    fail_reason = (
                        f"Existing database record in '{table_name}' matching unique key "
                        f"({found_lookup_key}='{found_lookup_val}') has an empty value for required column '{target_column}'."
                    )
            else:
                fail_reason = (
                    f"No existing database record found in table '{table_name}' matching lookup key "
                    f"({found_lookup_key}='{found_lookup_val}')."
                )
        elif found_lookup_key is None:
            if any(d in target_column.lower() for d in ("date", "time")):
                fail_reason = (
                    f"Required date/time column '{target_column}' in table '{table_name}' is empty and cannot "
                    f"silently substitute the current date/time without an approved business rule."
                )
            else:
                fail_reason = (
                    f"Required column '{target_column}' in table '{table_name}' is empty and no reliable unique "
                    f"lookup key is present in the source row to resolve it from the database."
                )
        else:
            fail_reason = (
                f"No existing records index available for table '{table_name}' to perform database lookup."
            )

        # 4. Attempt valid database default constraint
        if col_meta.default is not None:
            cleaned_default = extract_sql_default_value(col_meta.default)
            return MissingValueResolution(
                status="DEFAULT",
                value=cleaned_default,
                value_source="DATABASE_DEFAULT",
                lookup_column=None,
                lookup_value=None,
                message=(
                    f"Populated using database default constraint '{col_meta.default}' "
                    f"for required column '{table_name}.{target_column}'."
                )
            )

        # 5. Attempt approved deterministic business rules (if configured)
        if approved_business_rules:
            rule_key = f"{table_name}.{target_column}"
            if rule_key in approved_business_rules:
                rule_val = approved_business_rules[rule_key]
                return MissingValueResolution(
                    status="RESOLVED",
                    value=str(rule_val),
                    value_source="BUSINESS_RULE",
                    message=f"Populated using approved business rule for '{rule_key}'."
                )

        # 6. Cannot resolve safely: NOT NULL constraint violation prevention
        # Do not invent dummy names, IDs, or placeholders without an approved business rule.
        return MissingValueResolution(
            status="UNRESOLVED",
            value=None,
            value_source="UNRESOLVED",
            lookup_column=found_lookup_key,
            lookup_value=str(found_lookup_val) if found_lookup_val is not None else None,
            message=(
                f"Cannot insert NULL into NOT NULL column '{target_column}' in table '{table_name}'. {fail_reason}"
            )
        )

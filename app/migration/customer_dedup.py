"""
Customer-only duplicate detection and migration for dbo.Customer.

Duplicate rules (implemented once in ``CustomerIdentityIndex`` and shared by the
preview, the dry-run counts and the real migration):

| Condition                                   | Action                                   |
|---------------------------------------------|------------------------------------------|
| Customer Name + Mobile Number both match    | Duplicate; reuse existing Customer_ID    |
| Same name, different mobile                 | Insert a new customer                    |
| Different name, same mobile                 | Insert a new customer (both must match)  |
| Same name, both mobiles empty               | Duplicate; reuse existing Customer_ID    |
| Name empty, mobile present                  | Match by mobile; reuse if found          |
| Name empty, mobile not found                | Insert a new customer                    |
| Name and mobile both empty                  | Do not insert a customer (row skipped)   |

- Names compare trimmed and case-insensitively; mobiles compare by their digits.
- Missing Customer_Name is stored as an empty string, never the literal string 'null'.
- An existing customer is never overwritten by the migration.
- Foreign-key ID propagation remains available to related tables; other tables do not
  perform duplicate detection through this module.
"""

from __future__ import annotations
from app.migration.outlet_matcher import OutletCache
from datetime import datetime, date
import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple, Callable
import numpy as np
import pandas as pd
from sqlalchemy import text, Connection

from app.models.schema import TableMetadata, ColumnMetadata
from app.models.migration import (
    ColumnMappingResult,
    TableMigrationStat,
    ValueMappingItem
)
from app.migration.id_resolver import IdResolverStore
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship
from app.migration.engine import sanitize_value, coerce_value_for_column
from app.core.logging import logger


def _to_native_py(val: Any) -> Any:
    """Convert pandas/numpy types to pure Python primitives for SQL driver compatibility."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime()
    return val


def sanitize_customer_param_dict(
    param_dict: Dict[str, Any],
    t_meta: TableMetadata,
    table_name: str,
    col_meta_map: Dict[str, ColumnMetadata]
) -> None:
    """Ensure all datetime and typed columns in param_dict are properly sanitized."""
    for c in list(param_dict.keys()):
        c_meta = col_meta_map.get(c)
        if not c_meta:
            continue
        c_type = str(getattr(c_meta, "type", "") or "").upper()
        c_nullable = getattr(c_meta, "nullable", True)
        if any(t in c_type for t in ("DATE", "DATETIME", "DATETIME2", "SMALLDATETIME")):
            param_dict[c] = sanitize_value(param_dict[c], c_type, c_nullable)
            is_customer_modified_date = (
                str(table_name).lower().replace("dbo.", "").strip() == "customer"
                and normalized_column_name(c) in {"datemodified", "datemodify", "lastmodified", "updatedat"}
            )
            if (
                param_dict[c] is None
                and getattr(c_meta, "default", None) is not None
                and not is_customer_modified_date
            ):
                del param_dict[c]


def normalize_name(value: Any) -> str:
    """Normalize customer name for comparison."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.casefold() in {"", "nan", "none", "null", "nat"}:
        return ""
    return unicodedata.normalize("NFKC", s).casefold()


def normalize_mobile(value: Any) -> str:
    """Normalize mobile number for comparison.
    Trims whitespace and handles Excel numeric/string representations (.0, floats, etc.).
    Extracts purely numeric digit string.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        if value.is_integer():
            value = int(value)
    s = str(value).strip()
    if s.casefold() in {"", "nan", "none", "null", "nat"}:
        return ""
    if s.endswith(".0"):
        try:
            f = float(s)
            if f.is_integer():
                s = str(int(f))
        except ValueError:
            pass
    return re.sub(r"\D", "", s)


def is_name_blank_or_null(name_val: Any) -> bool:
    """Returns True if customer name is empty, whitespace, or literal string 'null'/'none'/'nan'."""
    if name_val is None:
        return True
    s = str(name_val).strip()
    return s == "" or s.lower() in ("null", "none", "nan", "nat")


def quote_identifier(name: str, dialect_name: str) -> str:
    """Quotes table or column identifiers appropriately for SQL Server, MySQL, Postgres, SQLite."""
    clean = str(name).strip().replace("[", "").replace("]", "").replace("`", "").replace('"', '')
    if dialect_name == "mssql":
        return f"[{clean}]"
    elif dialect_name in ("mysql", "mariadb"):
        return f"`{clean}`"
    else:
        return f'"{clean}"'


def is_text_sql_type(col_type: str) -> bool:
    """Return whether a schema type stores textual values."""
    type_upper = str(col_type or "").upper()
    return any(t in type_upper for t in ("CHAR", "TEXT", "CLOB", "STRING", "NCHAR", "VARCHAR", "NVARCHAR"))


def should_fill_unmapped_text_column(column: Any, table_meta: Any) -> bool:
    """Fill absent optional text columns with an empty string, not SQL NULL."""
    if getattr(column, "identity", False) or getattr(column, "default", None) is not None:
        return False
    if not is_text_sql_type(getattr(column, "type", "")):
        return False
    column_name = str(getattr(column, "name", ""))
    if getattr(column, "foreign_keys", None):
        return False
    if any(getattr(fk, "constrained_column", None) == column_name for fk in getattr(table_meta, "foreign_keys", [])):
        return False
    return True


def normalized_column_name(name: Any) -> str:
    """Normalize a database column name for case/punctuation-insensitive checks."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


_MOBILE_COLUMN_PRIORITY = (
    "mobilenumber", "mobile", "customermobile", "mobileno", "phonenumber",
    "customerphone", "phone", "contactnumber",
)
_MOBILE_COLUMN_KEYWORDS = (
    "mobile_number", "mobile", "phone_number", "customer_phone",
    "customer_mobile", "contact_number", "phone"
)


def detect_customer_mobile_column(t_meta: TableMetadata, mapped_db_columns: Optional[Set[str]] = None) -> str:
    """
    Pick the Customer column that stores the mobile number.

    A column mapped from the source file wins (so the duplicate check reads the same
    column the file writes); otherwise Mobile_Number-like names are preferred over
    generic Contact_Number/Phone columns. Previously the first keyword hit in table
    order was used, which selected Contact_Number on dbo.Customer even though the
    file's phone column is mapped to Mobile_Number, so existing customers were
    looked up in the wrong column.
    """
    candidates = [
        c.name for c in t_meta.columns
        if any(k in c.name.lower() for k in _MOBILE_COLUMN_KEYWORDS)
    ]
    if not candidates:
        return "Mobile_Number"

    def rank(col: str) -> int:
        norm = normalized_column_name(col)
        return _MOBILE_COLUMN_PRIORITY.index(norm) if norm in _MOBILE_COLUMN_PRIORITY else len(_MOBILE_COLUMN_PRIORITY)

    mapped = [c for c in candidates if mapped_db_columns and c in mapped_db_columns]
    pool = mapped or candidates
    return sorted(pool, key=lambda c: (rank(c), candidates.index(c)))[0]


class CustomerRuleConflictError(RuntimeError):
    """Raised when a Customer row cannot be stored without breaking the duplicate rules."""


class CustomerIdentityIndex:
    """
    Single source of truth for the Customer duplicate rules (used by preview,
    dry-run counts and the real migration so they can never disagree):

    | Incoming row                     | Matches                                      |
    |----------------------------------|----------------------------------------------|
    | name + mobile                    | customer with the SAME name AND SAME mobile  |
    | name, mobile empty               | customer with the same name and EMPTY mobile |
    | name empty, mobile               | any customer with that mobile                |
    | name empty, mobile empty         | nothing - the row is skipped                 |

    A name with a different mobile, or a different name with the same mobile,
    is a new customer. Entries are opaque objects (DB records or pending inserts);
    the first entry registered for a key wins, so existing DB rows (registered
    first, ordered by primary key) take precedence over new rows.
    """

    def __init__(self) -> None:
        self.by_pair: Dict[Tuple[str, str], Any] = {}
        self.by_name_without_mobile: Dict[str, Any] = {}
        self.by_mobile: Dict[str, Any] = {}

    @staticmethod
    def classify(norm_name: str, norm_mobile: str) -> str:
        if not norm_name and not norm_mobile:
            return "EMPTY_IDENTITY"
        if norm_name and norm_mobile:
            return "NAME_AND_MOBILE"
        if norm_name:
            return "NAME_ONLY"
        return "MOBILE_ONLY"

    def match(self, norm_name: str, norm_mobile: str) -> Any:
        kind = self.classify(norm_name, norm_mobile)
        if kind == "NAME_AND_MOBILE":
            return self.by_pair.get((norm_name, norm_mobile))
        if kind == "NAME_ONLY":
            return self.by_name_without_mobile.get(norm_name)
        if kind == "MOBILE_ONLY":
            # Prefer the exact identity (blank name + same mobile), else any
            # customer that owns this mobile number.
            exact = self.by_pair.get(("", norm_mobile))
            return exact if exact is not None else self.by_mobile.get(norm_mobile)
        return None

    def add(self, norm_name: str, norm_mobile: str, entry: Any) -> None:
        if norm_mobile:
            self.by_pair.setdefault((norm_name, norm_mobile), entry)
            self.by_mobile.setdefault(norm_mobile, entry)
        elif norm_name:
            self.by_name_without_mobile.setdefault(norm_name, entry)

    def replace(self, old_entry: Any, new_entry: Any) -> None:
        """Swap a pending placeholder for the real record after insert."""
        for mapping in (self.by_pair, self.by_name_without_mobile, self.by_mobile):
            for key, value in list(mapping.items()):
                if value is old_entry:
                    mapping[key] = new_entry


def customer_row_matches(
    incoming_name: Any, incoming_mobile: Any, existing_name: Any, existing_mobile: Any
) -> bool:
    """True when an existing customer is a duplicate of the incoming row under the rules."""
    index = CustomerIdentityIndex()
    marker = object()
    index.add(normalize_name(existing_name), normalize_mobile(existing_mobile), marker)
    return index.match(normalize_name(incoming_name), normalize_mobile(incoming_mobile)) is marker


def _find_rule_matching_customer(
    conn: Connection,
    dialect_name: str,
    table_name: str,
    pk_col: str,
    name_col: str,
    mobile_col: str,
    incoming_name: Any,
    incoming_mobile: Any,
) -> Any:
    """Look up an existing customer that is a rule-level duplicate of the incoming row."""
    quoted_tbl = quote_identifier(table_name, dialect_name)
    q_pk = quote_identifier(pk_col, dialect_name)
    q_nm = quote_identifier(name_col, dialect_name)
    q_mob = quote_identifier(mobile_col, dialect_name)
    n_mobile = normalize_mobile(incoming_mobile)
    if n_mobile:
        raw_mobile = str(incoming_mobile).strip()
        candidates = {raw_mobile, n_mobile}
        ph = [f":m_{k}" for k in range(len(candidates))]
        params = {f"m_{k}": v for k, v in enumerate(sorted(candidates))}
        sql = f"SELECT {q_pk}, {q_nm}, {q_mob} FROM {quoted_tbl} WHERE {q_mob} IN ({', '.join(ph)}) ORDER BY {q_pk} ASC"
    else:
        params = {"n_val": str(incoming_name or "").strip().lower()}
        sql = (
            f"SELECT {q_pk}, {q_nm}, {q_mob} FROM {quoted_tbl} "
            f"WHERE LOWER(LTRIM(RTRIM({q_nm}))) = :n_val ORDER BY {q_pk} ASC"
        )
    for row in conn.execute(text(sql), params).fetchall():
        if customer_row_matches(incoming_name, incoming_mobile, row[1], row[2]):
            return row[0]
    return None


def _is_unique_violation(err: Exception) -> bool:
    err_str = str(err).lower()
    return any(k in err_str for k in ("2627", "2601", "duplicate key", "unique constraint", "unique", "duplicate"))


def execute_customer_insert(
    conn: Connection,
    dialect_name: str,
    table_name: str,
    param_dict: Dict[str, Any],
    pk_col: str,
    t_meta: TableMetadata,
    source_row_num: int = 1,
    name_col: Optional[str] = None,
    mobile_col: Optional[str] = None,
) -> Tuple[Any, bool]:
    """
    Executes a parameterized INSERT for Customer and captures the generated identity.

    Returns ``(customer_id, reused_existing)``.

    On a unique-key violation the existing record is reused ONLY when it is a
    rule-level duplicate (same name+mobile, same name with no mobile, or same
    mobile when the incoming name is empty). An existing record is never
    overwritten. If no rule-level duplicate exists, the database schema itself
    prevents storing a customer that the rules call "new" (for example a UNIQUE
    constraint on Mobile_Number while two different names share one mobile);
    this raises ``CustomerRuleConflictError`` so the transaction is rolled back
    and the row is reported instead of silently linking orders to another person.
    """
    col_meta_map = {c.name: c for c in t_meta.columns}
    sanitize_customer_param_dict(param_dict, t_meta, table_name, col_meta_map)

    name_col = name_col or next((c.name for c in t_meta.columns if c.name.lower() in ("customer_name", "customername", "name")), "Customer_Name")
    mobile_col = mobile_col or next(
        (c.name for c in t_meta.columns if "mob" in c.name.lower() or "phone" in c.name.lower()), "Mobile_Number"
    )

    quoted_tbl = quote_identifier(table_name, dialect_name)
    quoted_pk = quote_identifier(pk_col, dialect_name)
    insert_cols = [c for c in param_dict.keys() if c in col_meta_map]
    quoted_cols = [quote_identifier(c, dialect_name) for c in insert_cols]
    val_params = [f":{c}" for c in insert_cols]
    params = {c: _to_native_py(param_dict[c]) for c in insert_cols}

    try:
        if dialect_name == "mssql":
            try:
                res = conn.execute(text(
                    f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                    f"OUTPUT INSERTED.{quoted_pk} "
                    f"VALUES ({', '.join(val_params)})"
                ), params)
                return res.scalar(), False
            except Exception as trigger_err:
                err_str = str(trigger_err).lower()
                if "trigger" not in err_str and "output clause without into" not in err_str:
                    raise
                res = conn.execute(text(
                    f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                    f"VALUES ({', '.join(val_params)});\n"
                    f"SELECT SCOPE_IDENTITY();"
                ), params)
                return res.scalar(), False
        if dialect_name in ("postgresql", "postgres"):
            res = conn.execute(text(
                f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                f"VALUES ({', '.join(val_params)}) RETURNING {quoted_pk}"
            ), params)
            return res.scalar(), False
        res = conn.execute(text(
            f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) VALUES ({', '.join(val_params)})"
        ), params)
        return getattr(res, "lastrowid", None) or param_dict.get(pk_col), False
    except Exception as insert_err:
        if not _is_unique_violation(insert_err):
            raise
        existing_cid = _find_rule_matching_customer(
            conn, dialect_name, table_name, pk_col, name_col, mobile_col,
            param_dict.get(name_col), param_dict.get(mobile_col),
        )
        if existing_cid is not None:
            logger.warning(
                "[CUSTOMER_REUSED_ON_UNIQUE_CONFLICT] source_row=%s Customer_ID=%s (existing record kept unchanged)",
                source_row_num, existing_cid,
            )
            return existing_cid, True
        raise CustomerRuleConflictError(
            f"Source row {source_row_num}: Customer (name={param_dict.get(name_col)!r}, "
            f"mobile={param_dict.get(mobile_col)!r}) is a NEW customer under the duplicate rules, "
            f"but table '{table_name}' rejected the insert with a unique-key violation and no existing "
            f"customer matches the rules. Check unique constraints on '{table_name}' "
            f"(e.g. on {mobile_col}). Database error: {insert_err}"
        ) from insert_err


def resolve_customer(
    conn: Connection,
    name: Any,
    mobile: Any,
    table_name: str = "Customer",
    dialect_name: Optional[str] = None
) -> Tuple[Optional[int], str]:
    """
    Resolve/insert a single customer row using the same rules as the migration.
    Returns (Customer_ID, action) where action is 'SKIPPED', 'REUSED' or 'INSERTED'.
    """
    if dialect_name is None:
        try:
            dialect_name = conn.dialect.name
        except Exception:
            dialect_name = "mssql"

    raw_name = "" if name is None else str(name).strip()
    raw_mobile = "" if mobile is None else str(mobile).strip()
    n_name = normalize_name(raw_name)
    n_mobile = normalize_mobile(raw_mobile)

    if not n_name and not n_mobile:
        logger.info("[CUSTOMER_SKIPPED_EMPTY_IDENTITY] Both customer name and mobile are empty; row skipped.")
        return None, "SKIPPED"

    quoted_tbl = quote_identifier(table_name, dialect_name)
    lock_clause = "WITH (UPDLOCK, HOLDLOCK)" if dialect_name == "mssql" else ""
    if n_mobile:
        where = "Mobile_Number IS NOT NULL AND LTRIM(RTRIM(Mobile_Number)) <> ''"
    else:
        where = "(Mobile_Number IS NULL OR LTRIM(RTRIM(Mobile_Number)) = '')"
    rows = conn.execute(text(
        f"SELECT Customer_ID, Customer_Name, Mobile_Number FROM {quoted_tbl} {lock_clause} "
        f"WHERE {where} ORDER BY Customer_ID ASC"
    )).mappings().all()
    index = CustomerIdentityIndex()
    for row in rows:
        index.add(normalize_name(row["Customer_Name"]), normalize_mobile(row["Mobile_Number"]), row["Customer_ID"])
    existing_id = index.match(n_name, n_mobile)
    if existing_id is not None:
        return existing_id, "REUSED"

    # Legacy helper contract: an unnamed customer is stored with the literal 'null'.
    insert_name = raw_name if n_name else "null"
    param_dict = {"Customer_Name": insert_name, "Mobile_Number": raw_mobile or None}
    dummy_meta = TableMetadata(
        name=table_name,
        schema_name="dbo",
        columns=[
            ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False),
            ColumnMetadata(name="Mobile_Number", type="VARCHAR(20)", nullable=True),
        ],
        primary_keys=["Customer_ID"]
    )
    new_id, reused = execute_customer_insert(
        conn, dialect_name, table_name, param_dict, "Customer_ID", dummy_meta,
        name_col="Customer_Name", mobile_col="Mobile_Number",
    )
    return new_id, ("REUSED" if reused else "INSERTED")


def process_customer_migration(
    conn: Connection,
    table_name: str,
    dialect_name: str,
    df: pd.DataFrame,
    df_indices: List[int],
    df_records: List[Dict[str, Any]],
    mappings: List[ColumnMappingResult],
    t_meta: TableMetadata,
    schema_tables: Dict[str, TableMetadata],
    t_stat: TableMigrationStat,
    id_store: IdResolverStore,
    lookup_relationships: List[LookupRelationship],
    parent_records_by_table: Dict[str, List[Dict[str, Any]]],
    val_items_by_col: Dict[str, Dict[str, List[ValueMappingItem]]],
    value_transform_lookup: Dict[Tuple[str, str, str], str],
    col_value_transform: Dict[Tuple[str, str], str],
    user_val_overrides: Dict[str, Any],
    fk_resolution_cache: Dict[Tuple[str, str, str, str], Any],
    upload_id: str,
    failure_details: List[Dict[str, Any]],
    generated_ids_by_table: Dict[str, List[Any]],
    source_row_indices_with_insert: Set[int],
    progress_callback: Optional[Callable[[str, int, int, int, int, int, int], None]] = None,
    tbl_idx: int = 0,
    total_tables: int = 1,
    outlet_id_cache: Optional[Dict[str, Any]] = None,
    file_id: Optional[int] = None
) -> Dict[str, int]:
    """
    Executes customer deduplication, insertion, and ID mapping within the migration engine transaction.
    Guarantees:
    - When name and mobile are both present, reuse only when both match.
    - When name is empty and mobile is present, match by mobile.
    - When mobile is empty and name is present, match only name-only customers.
    - Missing Customer_Name is stored as an empty string, never the literal 'null'.
    - If mobile is empty and name is present, matches only against customers with empty mobile.
    - Skips row if both name and mobile are empty.
    - Registers row-level Customer_ID in id_store for every row, ensuring Order table receives correct ID.
    """
    logger.info(f"[CUSTOMER_MIGRATION] Starting customer deduplication and migration for '{table_name}'")

    col_meta_map: Dict[str, ColumnMetadata] = {c.name: c for c in t_meta.columns}
    pk_cols = [c.name for c in t_meta.columns if c.primary_key]
    pk_col = pk_cols[0] if pk_cols else "Customer_ID"

    excel_to_db = {m.excel_column: m.database_column for m in mappings}
    db_to_excel = {m.database_column: m.excel_column for m in mappings if m.excel_column in df.columns}

    # 1. Identify customer name and mobile columns
    customer_name_db_col = next(
        (c.name for c in t_meta.columns if c.name.lower() in ("customer_name", "customername", "name")),
        "Customer_Name"
    )
    customer_mobile_db_col = detect_customer_mobile_column(t_meta, set(db_to_excel.keys()))
    # Earlier versions also wrote the mobile into the first phone-like column in table
    # order (Contact_Number on dbo.Customer). Keep writing it there so stored values
    # are unchanged; only the duplicate lookup moved to the mapped mobile column.
    legacy_mobile_db_col = next(
        (c.name for c in t_meta.columns if any(k in c.name.lower() for k in _MOBILE_COLUMN_KEYWORDS)),
        None
    )
    if legacy_mobile_db_col in (customer_mobile_db_col, customer_name_db_col) or legacy_mobile_db_col in db_to_excel:
        legacy_mobile_db_col = None

    name_ex_col = db_to_excel.get(customer_name_db_col)
    if not name_ex_col:
        for c in df.columns:
            c_clean = str(c).strip().lower().replace("_", "").replace(" ", "")
            if c_clean in ("customername", "name", "clientname"):
                name_ex_col = c
                break

    mobile_ex_col = db_to_excel.get(customer_mobile_db_col)
    if not mobile_ex_col:
        for c in df.columns:
            c_clean = str(c).strip().lower().replace("_", "").replace(" ", "")
            if any(k in c_clean for k in ("mobilenumber", "mobile", "phonenumber", "contactnumber", "phone")):
                mobile_ex_col = c
                break

    col_lookup = {c.lower(): c for c in df.columns}

    def get_df_val(row_dict: Dict[str, Any], ex_col_name: Optional[str]) -> Any:
        if not ex_col_name:
            return None
        if ex_col_name in row_dict:
            return row_dict[ex_col_name]
        actual_col = col_lookup.get(ex_col_name.strip().lower())
        if actual_col and actual_col in row_dict:
            return row_dict[actual_col]
        return None

    # Visit-date handling: prefer the Excel column mapped to Customer.date_added.
    # If it is not mapped, use an unambiguous, common visit/transaction date column.
    def normalized_db_column(name: Any) -> str:
        # Accept Date_Added/date_added and Date_Modified/date_modified variants.
        return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())

    date_added_db_col = next(
        (c.name for c in t_meta.columns if normalized_db_column(c.name) in {"dateadded", "datecreated"}),
        None
    )
    date_modify_db_col = next(
        (c.name for c in t_meta.columns if normalized_db_column(c.name) in {"datemodified", "datemodify", "lastmodified", "updatedat"}),
        None
    )
    visit_date_ex_col = db_to_excel.get(date_added_db_col) if date_added_db_col else None
    if not visit_date_ex_col:
        date_aliases = {
            "visitdate", "visit_date", "date", "transactiondate",
            "transaction_date", "orderdate", "order_date", "billdate",
            "bill_date", "createddate", "created_date"
        }
        for source_col in df.columns:
            normalized_col = str(source_col).strip().lower().replace(" ", "")
            if normalized_col in date_aliases:
                visit_date_ex_col = source_col
                break

    def get_visit_date(row_dict: Dict[str, Any]) -> Any:
        if not visit_date_ex_col:
            return None
        value = get_df_val(row_dict, visit_date_ex_col)
        if value is None or (isinstance(value, str) and value.strip().casefold() in {"", "nan", "none", "null", "nat"}):
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        return value

    def sanitize_customer_date(column_name: str, value: Any) -> Any:
        meta = col_meta_map.get(column_name)
        if meta is None:
            return value
        return sanitize_value(
            value,
            str(getattr(meta, "type", "") or "").upper(),
            getattr(meta, "nullable", True),
            allow_literal_null_str=False
        )

    def set_insert_visit_dates(param_dict: Dict[str, Any], row_dict: Dict[str, Any]) -> None:
        visit_date = get_visit_date(row_dict)
        if date_added_db_col:
            if visit_date is not None:
                param_dict[date_added_db_col] = sanitize_customer_date(date_added_db_col, visit_date)
            elif date_added_db_col in param_dict:
                param_dict[date_added_db_col] = sanitize_customer_date(
                    date_added_db_col, param_dict[date_added_db_col]
                )
        # New customers have no repeat-visit date yet.
        if date_modify_db_col:
            param_dict[date_modify_db_col] = None

    unmapped_empty_cols = [c.name for c in t_meta.columns if should_fill_unmapped_text_column(c, t_meta)]

    def _date_sort_key(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (datetime, date)):
            return value
        if isinstance(value, str):
            val_s = value.strip()
            if not val_s or val_s.casefold() in {"nan", "none", "null", "nat"}:
                return None
            try:
                return datetime.fromisoformat(val_s)
            except Exception:
                pass
        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return None
            return parsed.to_pydatetime()
        except (TypeError, ValueError, OverflowError):
            return None

    def update_repeat_visit_date(customer_id: Any, row_dict: Dict[str, Any]) -> None:
        visit_date = get_visit_date(row_dict)
        if not date_modify_db_col or visit_date is None or customer_id is None:
            return
        clean_date = sanitize_customer_date(date_modify_db_col, visit_date)
        if clean_date is None:
            return
        previous = pending_repeat_dates.get(customer_id)
        if previous is None:
            pending_repeat_dates[customer_id] = clean_date
            return
        new_key = _date_sort_key(clean_date)
        old_key = _date_sort_key(previous)
        if new_key is not None and (old_key is None or new_key > old_key):
            pending_repeat_dates[customer_id] = clean_date

    def update_pending_repeat_visit_date(p_idx: int, row_dict: Dict[str, Any]) -> None:
        visit_date = get_visit_date(row_dict)
        if not date_modify_db_col or visit_date is None:
            return
        clean_date = sanitize_customer_date(date_modify_db_col, visit_date)
        if clean_date is None:
            return
        previous = pending_new_cust_repeat_dates.get(p_idx)
        if previous is None:
            pending_new_cust_repeat_dates[p_idx] = clean_date
            return
        new_key = _date_sort_key(clean_date)
        old_key = _date_sort_key(previous)
        if new_key is not None and (old_key is None or new_key > old_key):
            pending_new_cust_repeat_dates[p_idx] = clean_date

    def flush_repeat_visit_dates() -> None:
        if not pending_repeat_dates or not date_modify_db_col:
            return
        quoted_modify = quote_identifier(date_modify_db_col, dialect_name)
        quoted_pk_local = quote_identifier(pk_col, dialect_name)
        date_added_condition = ""
        if date_added_db_col:
            quoted_added = quote_identifier(date_added_db_col, dialect_name)
            # Keep the original rule: Date_Modified only advances for visits later
            # than Date_Added and never overwrites a newer Date_Modified.
            date_added_condition = f" AND {quoted_added} IS NOT NULL AND {quoted_added} < :visit_date"
        update_stmt = text(
            f"UPDATE {quoted_tbl} SET {quoted_modify} = :visit_date "
            f"WHERE {quoted_pk_local} = :customer_id "
            f"AND ({quoted_modify} IS NULL OR {quoted_modify} < :visit_date)"
            f"{date_added_condition}"
        )
        params = [
            {"visit_date": visit_date, "customer_id": customer_id}
            for customer_id, visit_date in pending_repeat_dates.items()
        ]
        if params:
            conn.execute(update_stmt, params)
            logger.info(
                "[CUSTOMER_REPEAT_VISIT_DATE_BATCHED] processed=%s",
                len(params)
            )
        pending_repeat_dates.clear()

    # 2. Customer identity index (single implementation of the duplicate rules)
    identity_index = CustomerIdentityIndex()
    pending_repeat_dates: Dict[Any, Any] = {}
    pending_new_cust_repeat_dates: Dict[int, Any] = {}
    pending_customers: List[Dict[str, Any]] = []
    # Structured accounting (row-level, every source row lands in exactly one bucket)
    rows_linked_existing: List[int] = []
    rows_linked_new: List[int] = []
    rows_skipped_empty: List[int] = []
    rows_reused_on_conflict: List[int] = []

    quoted_tbl = quote_identifier(table_name, dialect_name)
    file_id_col = next((c.name for c in t_meta.columns if c.name.lower() == "fileid"), None)

    # Pre-load existing database customers that could be rule-level duplicates of
    # this upload: every customer sharing a name with the file (for name+mobile and
    # name-only rows) and every customer sharing a mobile (for mobile-only rows).
    # A failure here MUST stop the migration: continuing would insert duplicates of
    # every existing customer and the run would still look successful.
    select_cols = [
        c.name for c in t_meta.columns
        if not any(b in str(c.type).upper() for b in ("BINARY", "IMAGE", "VARBINARY", "GEOGRAPHY", "GEOMETRY"))
    ]
    for required_col in (pk_col, customer_name_db_col, customer_mobile_db_col):
        if required_col in col_meta_map and required_col not in select_cols:
            select_cols.append(required_col)
    quoted_select_cols = [quote_identifier(c, dialect_name) for c in select_cols]
    lock_clause = "WITH (UPDLOCK, HOLDLOCK)" if dialect_name == "mssql" else ""
    order_clause = f"ORDER BY {quote_identifier(pk_col, dialect_name)} ASC"

    distinct_mobile_keys: Set[str] = set()
    distinct_name_keys: Set[str] = set()
    for r_rec in df_records:
        rm = get_df_val(r_rec, mobile_ex_col)
        rn = get_df_val(r_rec, name_ex_col)
        nm = normalize_mobile(rm)
        nn = normalize_name(rn)
        if nm:
            distinct_mobile_keys.add(nm)
            distinct_mobile_keys.add(str(rm).strip())
        if nn:
            # Compared with LOWER(LTRIM(RTRIM(name))) so the lookup does not depend
            # on the database collation or on stray spaces in the source file.
            distinct_name_keys.add(str(rn).strip().lower())

    existing_by_id: Dict[Any, Dict[str, Any]] = {}
    q_mob = quote_identifier(customer_mobile_db_col, dialect_name)
    q_nm = quote_identifier(customer_name_db_col, dialect_name)
    PARAM_CHUNK = 800  # stays far below SQL Server's 2,100 parameter limit
    try:
        for keys, where_expr, prefix in (
            (sorted(distinct_mobile_keys), q_mob, "m"),
            (sorted(distinct_name_keys), f"LOWER(LTRIM(RTRIM({q_nm})))", "n"),
        ):
            for c_idx in range(0, len(keys), PARAM_CHUNK):
                chunk = keys[c_idx:c_idx + PARAM_CHUNK]
                ph = [f":{prefix}_{k}" for k in range(len(chunk))]
                p_chunk = {f"{prefix}_{k}": val for k, val in enumerate(chunk)}
                sel_sql = (
                    f"SELECT {', '.join(quoted_select_cols)} FROM {quoted_tbl} {lock_clause} "
                    f"WHERE {where_expr} IN ({', '.join(ph)}) {order_clause}"
                )
                for r in conn.execute(text(sel_sql), p_chunk).mappings().all():
                    if r.get(pk_col) is not None:
                        existing_by_id.setdefault(r.get(pk_col), dict(r))
    except Exception as prefetch_err:
        logger.error(f"[CUSTOMER_PREFETCH_FAILED] upload_id={upload_id}: {prefetch_err}")
        raise RuntimeError(
            f"Could not read existing customers from '{table_name}' to apply the duplicate rules; "
            f"migration stopped to avoid inserting duplicate customers. Database error: {prefetch_err}"
        ) from prefetch_err

    def _sort_pk(v: Any) -> Tuple[int, Any]:
        try:
            return (0, int(v))
        except (TypeError, ValueError):
            return (1, str(v))

    for db_id in sorted(existing_by_id.keys(), key=_sort_pk):
        r_dict = existing_by_id[db_id]
        raw_db_name = r_dict.get(customer_name_db_col)
        raw_db_mobile = r_dict.get(customer_mobile_db_col)
        entry = {"id": db_id, "name": raw_db_name, "mobile": raw_db_mobile, "record": r_dict}
        identity_index.add(normalize_name(raw_db_name), normalize_mobile(raw_db_mobile), entry)
    logger.info(
        "[CUSTOMER_PREFETCH] upload_id=%s existing_candidates=%s distinct_file_names=%s distinct_file_mobiles=%s",
        upload_id, len(existing_by_id), len(distinct_name_keys), len({normalize_mobile(m) for m in distinct_mobile_keys}),
    )

    # Discover foreign keys constrained on Customer (e.g. Outlet_ID)
    fk_injections: Dict[str, Tuple[str, str]] = {
        fk.constrained_column: (fk.target_table, fk.target_column)
        for fk in t_meta.foreign_keys
        if fk.target_table in schema_tables
    }
    for rel in lookup_relationships:
        if rel.child_table == table_name and rel.parent_table in schema_tables:
            if rel.child_column not in fk_injections:
                fk_injections[rel.child_column] = (rel.parent_table, rel.parent_id_column)

    def _register_existing_row(target_id: Any, orig_idx: int, norm_name: str, norm_mobile: str, raw_mobile: Any) -> None:
        id_store.register_row_id(table_name, int(orig_idx), target_id)
        if norm_mobile:
            id_store.register_id(table_name, norm_mobile, target_id)
            if raw_mobile:
                id_store.register_id(table_name, str(raw_mobile).strip(), target_id)
        if norm_name:
            id_store.register_id(table_name, norm_name, target_id)
        id_store.register_composite_key(table_name, (norm_name, norm_mobile), target_id)

    # 3. Pass 1: in-memory classification against DB customers + earlier rows of this file
    num_rows = len(df_records)
    for i in range(num_rows):
        orig_idx = df_indices[i]
        row = df_records[i]
        source_row_num = int(orig_idx) + 1

        raw_name = get_df_val(row, name_ex_col)
        raw_mobile = get_df_val(row, mobile_ex_col)
        norm_name = normalize_name(raw_name)
        norm_mobile = normalize_mobile(raw_mobile)

        # Rule: name and mobile both empty -> do not insert a customer.
        if not norm_name and not norm_mobile:
            logger.debug(
                "[CUSTOMER_SKIPPED_EMPTY_IDENTITY] upload_id=%s source_row=%s",
                upload_id, source_row_num
            )
            t_stat.skipped += 1
            rows_skipped_empty.append(source_row_num)
            continue

        customer_match = identity_index.match(norm_name, norm_mobile)
        if customer_match is not None:
            if "id" in customer_match:
                target_id = customer_match["id"]
                update_repeat_visit_date(target_id, row)
                _register_existing_row(target_id, orig_idx, norm_name, norm_mobile, raw_mobile)
                rows_linked_existing.append(source_row_num)
            else:
                p_idx = customer_match["pending_index"]
                pending_customers[p_idx]["row_indices"].append(int(orig_idx))
                update_pending_repeat_visit_date(p_idx, row)
                rows_linked_new.append(source_row_num)
            t_stat.reused += 1
        else:
            # New customer: name+mobile pair not seen, name-only not seen, or mobile not seen.
            insert_name = str(raw_name).strip() if norm_name else ""
            if norm_mobile:
                insert_mobile = str(raw_mobile).strip() if raw_mobile is not None and not is_name_blank_or_null(raw_mobile) else norm_mobile
            else:
                insert_mobile = None

            param_dict = _build_customer_param_dict(
                row=row,
                table_name=table_name,
                t_meta=t_meta,
                col_meta_map=col_meta_map,
                excel_to_db=excel_to_db,
                db_to_excel=db_to_excel,
                customer_name_db_col=customer_name_db_col,
                customer_mobile_db_col=customer_mobile_db_col,
                insert_name=insert_name,
                insert_mobile=insert_mobile,
                fk_injections=fk_injections,
                lookup_relationships=lookup_relationships,
                parent_records_by_table=parent_records_by_table,
                val_items_by_col=val_items_by_col,
                value_transform_lookup=value_transform_lookup,
                col_value_transform=col_value_transform,
                user_val_overrides=user_val_overrides,
                fk_resolution_cache=fk_resolution_cache,
                upload_id=upload_id,
                source_row_num=source_row_num,
                get_df_val=get_df_val,
                unmapped_empty_cols=unmapped_empty_cols,
                outlet_id_cache=outlet_id_cache,
                file_id=file_id,
                file_id_col=file_id_col
            )

            if legacy_mobile_db_col:
                param_dict[legacy_mobile_db_col] = insert_mobile
            set_insert_visit_dates(param_dict, row)
            p_idx = len(pending_customers)
            placeholder = {"pending_index": p_idx}
            pending_customers.append({
                "pending_id": p_idx,
                "placeholder": placeholder,
                "norm_mobile": norm_mobile,
                "raw_mobile": raw_mobile if norm_mobile else None,
                "norm_name": norm_name,
                "raw_name": raw_name,
                "insert_name": insert_name,
                "insert_mobile": insert_mobile,
                "param_dict": param_dict,
                "source_row_num": source_row_num,
                "primary_row_idx": int(orig_idx),
                "row_indices": [int(orig_idx)]
            })
            identity_index.add(norm_name, norm_mobile, placeholder)
            rows_linked_new.append(source_row_num)

        # Periodic progress callback
        if progress_callback and (i % 500 == 0 or i == num_rows - 1):
            progress_callback(
                table_name,
                i + 1,
                num_rows,
                t_stat.inserted,
                t_stat.reused,
                tbl_idx + 1,
                total_tables
            )

    # -------------------------------------------------------------
    # Pass 2: Multi-row batch INSERT for new customers
    # -------------------------------------------------------------
    def _register_inserted_customer(new_id: Any, p_cust: Dict[str, Any], reused_existing: bool = False) -> None:
        if new_id is None:
            raise RuntimeError(
                f"Customer insert for source row {p_cust['source_row_num']} did not return a Customer_ID; "
                f"related orders cannot be linked."
            )
        if isinstance(new_id, float) and new_id.is_integer():
            new_id = int(new_id)
        elif str(new_id).endswith(".0"):
            new_id = int(float(new_id))

        for idx in p_cust["row_indices"]:
            id_store.register_row_id(table_name, idx, new_id)
        if p_cust["norm_mobile"]:
            id_store.register_id(table_name, p_cust["norm_mobile"], new_id)
            if p_cust["raw_mobile"]:
                id_store.register_id(table_name, str(p_cust["raw_mobile"]).strip(), new_id)
        if p_cust["norm_name"]:
            id_store.register_id(table_name, p_cust["norm_name"], new_id)
        id_store.register_composite_key(table_name, (p_cust["norm_name"], p_cust["norm_mobile"]), new_id)
        identity_index.replace(
            p_cust["placeholder"],
            {"id": new_id, "name": p_cust["insert_name"], "mobile": p_cust["insert_mobile"], "record": p_cust["param_dict"]},
        )

        if p_cust["pending_id"] in pending_new_cust_repeat_dates:
            pending_repeat_dates[new_id] = pending_new_cust_repeat_dates[p_cust["pending_id"]]

        if reused_existing:
            # Existing rule-level duplicate found by the database (not in prefetch);
            # it is linked, never overwritten, and is not counted as an insert.
            t_stat.reused += 1
            rows_reused_on_conflict.append(p_cust["source_row_num"])
        else:
            generated_ids_by_table[table_name].append(new_id)
            source_row_indices_with_insert.add(p_cust["primary_row_idx"])
            t_stat.inserted += 1

    def _insert_single(p_cust: Dict[str, Any]) -> None:
        new_id, reused = execute_customer_insert(
            conn=conn,
            dialect_name=dialect_name,
            table_name=table_name,
            param_dict=p_cust["param_dict"],
            pk_col=pk_col,
            t_meta=t_meta,
            source_row_num=p_cust["source_row_num"],
            name_col=customer_name_db_col,
            mobile_col=customer_mobile_db_col,
        )
        _register_inserted_customer(new_id, p_cust, reused_existing=reused)

    if pending_customers:
        for p_cust in pending_customers:
            sanitize_customer_param_dict(p_cust["param_dict"], t_meta, table_name, col_meta_map)

        customers_by_cols: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
        for p_cust in pending_customers:
            cols_key = tuple(c for c in p_cust["param_dict"].keys() if c in col_meta_map and not getattr(col_meta_map[c], "identity", False))
            customers_by_cols.setdefault(cols_key, []).append(p_cust)

        quoted_pk = quote_identifier(pk_col, dialect_name)

        for cols_tuple, cust_chunk_list in customers_by_cols.items():
            cols = list(cols_tuple)
            quoted_cols = [quote_identifier(c, dialect_name) for c in cols]
            max_cols = max(1, len(cols))
            CHUNK_SIZE = max(1, min(200, 2000 // max_cols))

            def _insert_cust_subchunk(subchunk: List[Dict[str, Any]]) -> None:
                if not subchunk:
                    return
                if len(subchunk) == 1:
                    _insert_single(subchunk[0])
                    return

                val_clauses = []
                batch_params = {}
                for r_idx, p_cust in enumerate(subchunk):
                    row_placeholders = []
                    for c in cols:
                        param_name = f"{c}_{r_idx}"
                        row_placeholders.append(f":{param_name}")
                        batch_params[param_name] = coerce_value_for_column(_to_native_py(p_cust["param_dict"].get(c)), col_meta_map.get(c))
                    val_clauses.append(f"({', '.join(row_placeholders)})")

                if dialect_name == "mssql":
                    sql = (
                        f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)})\n"
                        f"OUTPUT INSERTED.{quoted_pk}\n"
                        f"VALUES {', '.join(val_clauses)};"
                    )
                elif dialect_name in ("postgresql", "postgres", "sqlite"):
                    sql = f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) VALUES {', '.join(val_clauses)} RETURNING {quoted_pk}"
                else:
                    sql = None

                if sql:
                    try:
                        fetched_rows = conn.execute(text(sql), batch_params).fetchall()
                    except Exception as batch_err:
                        if not _is_unique_violation(batch_err):
                            raise
                        # The whole multi-row statement failed atomically: bisect so
                        # the conflicting row(s) are handled individually (reuse an
                        # exact rule match, otherwise fail loudly - never overwrite).
                        logger.warning(f"[CUSTOMER_BATCH_FALLBACK] {batch_err}")
                        mid = len(subchunk) // 2
                        _insert_cust_subchunk(subchunk[:mid])
                        _insert_cust_subchunk(subchunk[mid:])
                        return
                    if len(fetched_rows) != len(subchunk):
                        raise RuntimeError(
                            f"Customer batch insert returned {len(fetched_rows)} IDs for {len(subchunk)} rows "
                            f"(source rows {[p['source_row_num'] for p in subchunk][:20]}); cannot link orders safely."
                        )
                    # SQL Server does not guarantee OUTPUT order for multi-row VALUES;
                    # SQLite/Postgres RETURNING follow VALUES order in practice. Map IDs
                    # back by reading the key columns of the inserted rows.
                    if dialect_name == "mssql":
                        id_list = [r[0] for r in fetched_rows]
                        ph = [f":vid_{k}" for k in range(len(id_list))]
                        back = conn.execute(
                            text(
                                f"SELECT {quoted_pk}, {q_nm}, {q_mob} FROM {quoted_tbl} "
                                f"WHERE {quoted_pk} IN ({', '.join(ph)})"
                            ),
                            {f"vid_{k}": _to_native_py(v) for k, v in enumerate(id_list)},
                        ).fetchall()
                        by_identity: Dict[Tuple[str, str], List[Any]] = {}
                        for b in sorted(back, key=lambda x: _sort_pk(x[0])):
                            by_identity.setdefault((normalize_name(b[1]), normalize_mobile(b[2])), []).append(b[0])
                        for p_cust in subchunk:
                            ids = by_identity.get((p_cust["norm_name"], p_cust["norm_mobile"])) or []
                            if not ids:
                                raise RuntimeError(
                                    f"Inserted customer for source row {p_cust['source_row_num']} could not be "
                                    f"located after insert; cannot link orders safely."
                                )
                            _register_inserted_customer(ids.pop(0), p_cust)
                    else:
                        for r_fetch, p_cust in zip(fetched_rows, subchunk):
                            _register_inserted_customer(r_fetch[0], p_cust)
                    return

                for p_cust in subchunk:
                    _insert_single(p_cust)

            for chunk_start in range(0, len(cust_chunk_list), CHUNK_SIZE):
                chunk = cust_chunk_list[chunk_start : chunk_start + CHUNK_SIZE]
                _insert_cust_subchunk(chunk)

                if progress_callback:
                    progress_callback(
                        table_name,
                        min(num_rows, chunk_start + len(chunk)),
                        num_rows,
                        t_stat.inserted,
                        t_stat.reused,
                        tbl_idx + 1,
                        total_tables
                    )

    # -------------------------------------------------------------
    # Pass 3: Batched repeat-visit dates
    # -------------------------------------------------------------
    flush_repeat_visit_dates()

    # Structured, row-level reconciliation. Every source row must be in exactly one bucket.
    accounted = len(rows_linked_existing) + len(rows_linked_new) + len(rows_skipped_empty)
    if accounted != num_rows:
        raise RuntimeError(
            f"Customer accounting mismatch for upload {upload_id}: {num_rows} source rows but only "
            f"{accounted} classified (existing={len(rows_linked_existing)}, new={len(rows_linked_new)}, "
            f"skipped={len(rows_skipped_empty)})."
        )
    logger.info(
        "[CUSTOMER_RECONCILIATION] table=%s upload_id=%s source_rows=%s | "
        "distinct_new_customers_inserted=%s | rows_linked_to_new_customers=%s | "
        "rows_linked_to_existing_db_customers=%s | rows_reused_after_unique_conflict=%s | "
        "rows_skipped_name_and_mobile_empty=%s (expected business rule) | failures=%s | "
        "sample_skipped_rows=%s",
        table_name, upload_id, num_rows, t_stat.inserted, len(rows_linked_new),
        len(rows_linked_existing), len(rows_reused_on_conflict), len(rows_skipped_empty),
        t_stat.failed, rows_skipped_empty[:20],
    )
    return {
        "inserted": t_stat.inserted,
        "reused": t_stat.reused,
        "updated": t_stat.updated,
        "skipped": t_stat.skipped
    }


def _build_customer_param_dict(
    row: Dict[str, Any],
    table_name: str,
    t_meta: TableMetadata,
    col_meta_map: Dict[str, ColumnMetadata],
    excel_to_db: Dict[str, str],
    db_to_excel: Dict[str, str],
    customer_name_db_col: str,
    customer_mobile_db_col: str,
    insert_name: str,
    insert_mobile: Optional[str],
    fk_injections: Dict[str, Tuple[str, str]],
    lookup_relationships: List[LookupRelationship],
    parent_records_by_table: Dict[str, List[Dict[str, Any]]],
    val_items_by_col: Dict[str, Dict[str, List[ValueMappingItem]]],
    value_transform_lookup: Dict[Tuple[str, str, str], str],
    col_value_transform: Dict[Tuple[str, str], str],
    user_val_overrides: Dict[str, Any],
    fk_resolution_cache: Dict[Tuple[str, str, str, str], Any],
    upload_id: str,
    source_row_num: int,
    get_df_val: Callable[[Dict[str, Any], Optional[str]], Any],
    unmapped_empty_cols: Optional[List[str]] = None,
    outlet_id_cache: Optional[Dict[str, Any]] = None,
    file_id: Optional[int] = None,
    file_id_col: Optional[str] = None
) -> Dict[str, Any]:
    """Builds a complete parameter dictionary for Customer insertion."""
    param_dict: Dict[str, Any] = {
        customer_name_db_col: insert_name,
        customer_mobile_db_col: insert_mobile
    }

    # Map other mapped columns for Customer
    for ex_col, db_col in excel_to_db.items():
        if db_col in (customer_name_db_col, customer_mobile_db_col):
            continue
        c_meta = col_meta_map.get(db_col)
        if not c_meta or getattr(c_meta, "identity", False):
            continue
        raw_val = get_df_val(row, ex_col)
        raw_str = str(raw_val).strip() if raw_val is not None else ""
        mapped_val = (
            value_transform_lookup.get((table_name, db_col, raw_str))
            or col_value_transform.get((db_col, raw_str))
        )
        val_to_use = mapped_val if mapped_val is not None else raw_val

        # Type-safe value sanitization (handles dates, numbers, text, nullability)
        c_type = str(getattr(c_meta, "type", "") or "").upper()
        c_nullable = getattr(c_meta, "nullable", True)
        param_dict[db_col] = sanitize_value(val_to_use, c_type, c_nullable, allow_literal_null_str=False)

    # Resolve foreign keys on Customer (e.g. Outlet_ID) with cache
    for fk_col, (parent_tbl, parent_col) in fk_injections.items():
        if fk_col in param_dict and param_dict[fk_col] is not None:
            continue
        if str(parent_tbl or "").strip().lower().split(".")[-1] == "outlets":
            if "outlet_cache" not in fk_resolution_cache:
                o_recs = parent_records_by_table.get(parent_tbl) or parent_records_by_table.get(f"dbo.{parent_tbl}") or []
                fk_resolution_cache["outlet_cache"] = OutletCache(o_recs)
            o_cache = fk_resolution_cache["outlet_cache"]
            rel_o = next((r for r in lookup_relationships if r.child_table == table_name and r.child_column == fk_col), None)
            src_o = get_df_val(row, rel_o.excel_name_column) if (rel_o and rel_o.excel_name_column) else None
            oid = o_cache.find_outlet_id(src_o) if src_o else None
            if oid is None:
                oid = o_cache.resolve_row_outlet_id(row if isinstance(row, dict) else row.to_dict())
            if oid is not None:
                param_dict[fk_col] = int(oid)
                continue

        db_recs = parent_records_by_table.get(parent_tbl, [])
        rel = next((r for r in lookup_relationships if r.child_table == table_name and r.child_column == fk_col), None)
        src_name = get_df_val(row, rel.excel_name_column) if (rel and rel.excel_name_column) else None
        str_name = str(src_name).strip() if src_name is not None else None

        user_override = (
            user_val_overrides.get(f"{parent_tbl}.{str_name}")
            or user_val_overrides.get(f"{table_name}.{fk_col}")
            or user_val_overrides.get(str_name)
        )
        fk_cache_key = (parent_tbl, parent_col, str_name or "", str(user_override or ""))
        if fk_cache_key in fk_resolution_cache:
            res_rec = fk_resolution_cache[fk_cache_key]
        else:
            res_rec = DynamicLookupResolver.resolve_foreign_key_record(
                child_table=table_name,
                child_column=fk_col,
                parent_table=parent_tbl,
                parent_id_column=parent_col,
                parent_name_column=rel.parent_name_column if rel else None,
                db_records=db_recs,
                excel_source_value=str_name,
                row_resolved_entities={},
                row_value_mappings=[],
                row_data=row if isinstance(row, dict) else row.to_dict(),
                user_override_id=user_override,
                all_parent_records=parent_records_by_table,
                upload_id=upload_id,
                source_row_num=source_row_num
            )
            if res_rec.status == "RESOLVED":
                fk_resolution_cache[fk_cache_key] = res_rec

        if res_rec.status == "RESOLVED":
            param_dict[fk_col] = res_rec.matched_primary_key_value

    # Populate optional unmapped text columns with empty strings
    if unmapped_empty_cols is not None:
        for _c_name in unmapped_empty_cols:
            param_dict.setdefault(_c_name, "")
    else:
        for _col in t_meta.columns:
            if should_fill_unmapped_text_column(_col, t_meta):
                param_dict.setdefault(_col.name, "")

    if file_id_col and file_id is not None:
        param_dict[file_id_col] = file_id

    return param_dict


def preview_customer_counts(
    df: pd.DataFrame,
    existing_records: List[Dict[str, Any]],
    mappings: List[ColumnMappingResult],
    t_meta: TableMetadata
) -> Tuple[int, int, int]:
    """
    Simulates customer deduplication rules for Step 3 Preview without modifying the database.
    Returns: (planned_inserts, planned_skips, planned_reuses)
    """
    customer_name_db_col = next(
        (c.name for c in t_meta.columns if c.name.lower() in ("customer_name", "customername", "name")),
        "Customer_Name"
    )
    db_to_excel = {m.database_column: m.excel_column for m in mappings if m.excel_column in df.columns}
    customer_mobile_db_col = detect_customer_mobile_column(t_meta, set(db_to_excel.keys()))
    name_ex_col = db_to_excel.get(customer_name_db_col)
    mobile_ex_col = db_to_excel.get(customer_mobile_db_col)

    if not name_ex_col:
        for c in df.columns:
            c_clean = str(c).strip().lower().replace("_", "").replace(" ", "")
            if c_clean in ("customername", "name", "clientname"):
                name_ex_col = c
                break

    if not mobile_ex_col:
        for c in df.columns:
            c_clean = str(c).strip().lower().replace("_", "").replace(" ", "")
            if any(k in c_clean for k in ("mobilenumber", "mobile", "phonenumber", "contactnumber", "phone")):
                mobile_ex_col = c
                break

    col_lookup = {c.lower(): c for c in df.columns}

    def get_df_val(row_dict: Dict[str, Any], ex_col_name: Optional[str]) -> Any:
        if not ex_col_name:
            return None
        if ex_col_name in row_dict:
            return row_dict[ex_col_name]
        actual_col = col_lookup.get(ex_col_name.strip().lower())
        if actual_col and actual_col in row_dict:
            return row_dict[actual_col]
        return None

    # Same rule implementation as the real migration, seeded with existing DB customers.
    index = CustomerIdentityIndex()
    pk_name = next((c.name for c in t_meta.columns if c.primary_key), None)
    existing_sorted = list(existing_records or [])
    if pk_name:
        def _pk_key(rec: Dict[str, Any]) -> Tuple[int, Any]:
            v = rec.get(pk_name)
            try:
                return (0, int(v))
            except (TypeError, ValueError):
                return (1, str(v))
        existing_sorted.sort(key=_pk_key)
    for rec in existing_sorted:
        index.add(
            normalize_name(rec.get(customer_name_db_col)),
            normalize_mobile(rec.get(customer_mobile_db_col)),
            "existing",
        )

    ins_cnt = 0
    skip_cnt = 0
    reus_cnt = 0

    df_records = df.to_dict(orient="records")
    for row in df_records:
        n_n = normalize_name(get_df_val(row, name_ex_col))
        n_m = normalize_mobile(get_df_val(row, mobile_ex_col))
        if not n_n and not n_m:
            skip_cnt += 1
            continue
        if index.match(n_n, n_m) is not None:
            reus_cnt += 1
        else:
            ins_cnt += 1
            index.add(n_n, n_m, "new")

    return ins_cnt, skip_cnt, reus_cnt

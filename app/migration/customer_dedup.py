"""
Customer-only duplicate detection and migration for dbo.Customer.

Rules:
- Name and mobile both present: reuse only when both normalized values match.
- Name present and mobile different: insert a separate customer.
- Name empty and mobile present: match by normalized mobile.
- Name present and mobile empty: match only a customer with the same name and empty mobile.
- Name and mobile both empty: skip the customer row.
- Missing Customer_Name is stored as an empty string, never the literal string 'null'.
- Foreign-key ID propagation remains available to related tables; other tables do not
  perform duplicate detection through this module.
"""

from __future__ import annotations
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
from app.migration.engine import sanitize_value
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


def execute_customer_insert(
    conn: Connection,
    dialect_name: str,
    table_name: str,
    param_dict: Dict[str, Any],
    pk_col: str,
    t_meta: TableMetadata,
    source_row_num: int = 1
) -> Any:
    """Executes a parameterized INSERT for Customer and captures the generated identity."""
    col_meta_map = {c.name: c for c in t_meta.columns}
    sanitize_customer_param_dict(param_dict, t_meta, table_name, col_meta_map)

    quoted_tbl = quote_identifier(table_name, dialect_name)
    quoted_pk = quote_identifier(pk_col, dialect_name)
    insert_cols = [c for c in param_dict.keys() if c in col_meta_map]
    quoted_cols = [quote_identifier(c, dialect_name) for c in insert_cols]
    val_params = [f":{c}" for c in insert_cols]

    if dialect_name == "mssql":
        try:
            insert_stmt = text(
                f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                f"OUTPUT INSERTED.{quoted_pk} "
                f"VALUES ({', '.join(val_params)})"
            )
            res = conn.execute(insert_stmt, param_dict)
            return res.scalar(), False
        except Exception as trigger_err:
            err_str = str(trigger_err).lower()
            if "trigger" in err_str or "output clause without into" in err_str:
                fallback_stmt = text(
                    f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                    f"VALUES ({', '.join(val_params)});\n"
                    f"SELECT SCOPE_IDENTITY();"
                )
                try:
                    res = conn.execute(fallback_stmt, param_dict)
                    return res.scalar(), False
                except Exception as fb_err:
                    trigger_err = fb_err
                    err_str = str(fb_err).lower()

            if "2627" in err_str or "2601" in err_str or "duplicate key" in err_str or "primary key" in err_str or "unique constraint" in err_str:
                logger.warning(
                    f"[CUSTOMER_OVERRIDE_ON_DUPLICATE] Duplicate customer key on insert ({trigger_err}); overriding existing record."
                )
                mob_col = next((c.name for c in t_meta.columns if "mob" in c.name.lower() or "phone" in c.name.lower()), "Mobile_Number")
                name_col = next((c.name for c in t_meta.columns if "name" in c.name.lower()), "Customer_Name")
                existing_cid = param_dict.get(pk_col)
                if existing_cid is None and param_dict.get(mob_col):
                    q_mob = quote_identifier(mob_col, dialect_name)
                    existing_cid = conn.execute(
                        text(f"SELECT {quoted_pk} FROM {quoted_tbl} WHERE {q_mob} = :m_val"),
                        {"m_val": str(param_dict[mob_col]).strip()}
                    ).scalar()
                elif existing_cid is None and param_dict.get(name_col):
                    q_nm = quote_identifier(name_col, dialect_name)
                    existing_cid = conn.execute(
                        text(f"SELECT {quoted_pk} FROM {quoted_tbl} WHERE {q_nm} = :n_val"),
                        {"n_val": str(param_dict[name_col]).strip()}
                    ).scalar()

                if existing_cid is not None:
                    upd_cols = [c for c in insert_cols if c != pk_col]
                    quoted_upd = [f"{quote_identifier(c, dialect_name)} = :{c}" for c in upd_cols]
                    upd_stmt = text(f"UPDATE {quoted_tbl} SET {', '.join(quoted_upd)} WHERE {quoted_pk} = :_override_cid")
                    upd_params = dict(param_dict)
                    upd_params["_override_cid"] = existing_cid
                    conn.execute(upd_stmt, upd_params)
                    return existing_cid, True
            raise trigger_err
    elif dialect_name in ("postgresql", "postgres"):
        try:
            insert_stmt = text(
                f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                f"VALUES ({', '.join(val_params)}) "
                f"RETURNING {quoted_pk}"
            )
            res = conn.execute(insert_stmt, param_dict)
            return res.scalar(), False
        except Exception as pg_err:
            err_str = str(pg_err).lower()
            if "unique" in err_str or "duplicate" in err_str:
                mob_col = next((c.name for c in t_meta.columns if "mob" in c.name.lower() or "phone" in c.name.lower()), "Mobile_Number")
                existing_cid = param_dict.get(pk_col)
                if existing_cid is None and param_dict.get(mob_col):
                    existing_cid = conn.execute(
                        text(f"SELECT {quoted_pk} FROM {quoted_tbl} WHERE {quote_identifier(mob_col, dialect_name)} = :m_val"),
                        {"m_val": str(param_dict[mob_col]).strip()}
                    ).scalar()
                if existing_cid is not None:
                    upd_cols = [c for c in insert_cols if c != pk_col]
                    quoted_upd = [f"{quote_identifier(c, dialect_name)} = :{c}" for c in upd_cols]
                    upd_stmt = text(f"UPDATE {quoted_tbl} SET {', '.join(quoted_upd)} WHERE {quoted_pk} = :_override_cid")
                    upd_params = dict(param_dict)
                    upd_params["_override_cid"] = existing_cid
                    conn.execute(upd_stmt, upd_params)
                    return existing_cid, True
            raise pg_err
    else:  # sqlite and generic
        try:
            insert_stmt = text(
                f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) "
                f"VALUES ({', '.join(val_params)})"
            )
            res = conn.execute(insert_stmt, param_dict)
            return getattr(res, "lastrowid", None) or param_dict.get(pk_col), False
        except Exception as sql_err:
            err_str = str(sql_err).lower()
            if "unique" in err_str or "duplicate" in err_str:
                mob_col = next((c.name for c in t_meta.columns if "mob" in c.name.lower() or "phone" in c.name.lower()), "Mobile_Number")
                existing_cid = param_dict.get(pk_col)
                if existing_cid is None and param_dict.get(mob_col):
                    existing_cid = conn.execute(
                        text(f"SELECT {quoted_pk} FROM {quoted_tbl} WHERE {quote_identifier(mob_col, dialect_name)} = :m_val"),
                        {"m_val": str(param_dict[mob_col]).strip()}
                    ).scalar()
                if existing_cid is not None:
                    upd_cols = [c for c in insert_cols if c != pk_col]
                    quoted_upd = [f"{quote_identifier(c, dialect_name)} = :{c}" for c in upd_cols]
                    upd_stmt = text(f"UPDATE {quoted_tbl} SET {', '.join(quoted_upd)} WHERE {quoted_pk} = :_override_cid")
                    upd_params = dict(param_dict)
                    upd_params["_override_cid"] = existing_cid
                    conn.execute(upd_stmt, upd_params)
                    return existing_cid, True
            raise sql_err


def resolve_customer(
    conn: Connection,
    name: Any,
    mobile: Any,
    table_name: str = "Customer",
    dialect_name: Optional[str] = None
) -> Tuple[Optional[int], str]:
    """
    Resolve/insert a single customer row. Returns (Customer_ID, action).
    action can be: 'SKIPPED', 'REUSED', 'UPDATED_NAME', 'INSERTED'.
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

    # Rule 3: Both empty -> Skip
    if not n_name and not n_mobile:
        logger.info("[CUSTOMER_SKIPPED_EMPTY_IDENTITY] Both customer name and mobile are empty; row skipped.")
        return None, "SKIPPED"

    quoted_tbl = quote_identifier(table_name, dialect_name)
    lock_clause = "WITH (UPDLOCK, HOLDLOCK)" if dialect_name == "mssql" else ""

    # Rule 1: Mobile number is present
    if n_mobile:
        sql = f"""
            SELECT Customer_ID, Customer_Name, Mobile_Number
            FROM {quoted_tbl} {lock_clause}
            WHERE Mobile_Number IS NOT NULL AND LTRIM(RTRIM(Mobile_Number)) <> ''
            ORDER BY Customer_ID ASC
        """
        rows = conn.execute(text(sql)).mappings().all()
        for row in rows:
            if normalize_mobile(row["Mobile_Number"]) == n_mobile:
                cid = row["Customer_ID"]
                old_name = row["Customer_Name"]
                if is_name_blank_or_null(old_name) and n_name:
                    update_sql = f"UPDATE {quoted_tbl} SET Customer_Name = :name WHERE Customer_ID = :id"
                    conn.execute(text(update_sql), {"name": raw_name, "id": cid})
                    logger.info(f"[CUSTOMER_NAME_UPDATED] Customer_ID={cid} updated from {old_name!r} to {raw_name!r}")
                    return cid, "UPDATED_NAME"
                return cid, "REUSED"

        # Not found with mobile -> Insert new customer
        insert_name = raw_name if n_name else "null"
        insert_mobile = raw_mobile or None
        param_dict = {"Customer_Name": insert_name, "Mobile_Number": insert_mobile}
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
        insert_res = execute_customer_insert(conn, dialect_name, table_name, param_dict, "Customer_ID", dummy_meta)
        new_id = insert_res[0] if isinstance(insert_res, tuple) else insert_res
        return new_id, "INSERTED"

    # Rule 2: Mobile empty, customer name present
    if n_name and not n_mobile:
        sql = f"""
            SELECT Customer_ID, Customer_Name, Mobile_Number
            FROM {quoted_tbl} {lock_clause}
            WHERE (Mobile_Number IS NULL OR LTRIM(RTRIM(Mobile_Number)) = '')
            ORDER BY Customer_ID ASC
        """
        rows = conn.execute(text(sql)).mappings().all()
        for row in rows:
            if normalize_name(row["Customer_Name"]) == n_name:
                return row["Customer_ID"], "REUSED"

        # Not found -> Insert new name-only customer
        param_dict = {"Customer_Name": raw_name, "Mobile_Number": None}
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
        insert_res = execute_customer_insert(conn, dialect_name, table_name, param_dict, "Customer_ID", dummy_meta)
        new_id = insert_res[0] if isinstance(insert_res, tuple) else insert_res
        return new_id, "INSERTED"

    return None, "SKIPPED"


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
    outlet_id_cache: Optional[Dict[str, Any]] = None
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
    customer_mobile_db_col = next(
        (c.name for c in t_meta.columns if any(k in c.name.lower() for k in (
            "mobile_number", "mobile", "phone_number", "customer_phone",
            "customer_mobile", "contact_number", "phone"
        ))),
        "Mobile_Number"
    )

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

    # 2. In-memory customer tracking (Excel-scoped deduplication only; no DB pre-queries)
    mobile_to_customer: Dict[str, Dict[str, Any]] = {}
    name_to_customer: Dict[str, Dict[str, Any]] = {}
    name_mobile_to_customer: Dict[Tuple[str, str], Dict[str, Any]] = {}
    pending_repeat_dates: Dict[Any, Any] = {}
    pending_new_cust_repeat_dates: Dict[int, Any] = {}
    pending_name_updates: Dict[Any, str] = {}
    pending_customers: List[Dict[str, Any]] = []

    quoted_tbl = quote_identifier(table_name, dialect_name)

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

    # 3. Pass 1: High-speed in-memory classification and deduplication
    num_rows = len(df_records)
    for i in range(num_rows):
        orig_idx = df_indices[i]
        row = df_records[i]
        source_row_num = int(orig_idx) + 1

        raw_name = get_df_val(row, name_ex_col)
        raw_mobile = get_df_val(row, mobile_ex_col)
        norm_name = normalize_name(raw_name)
        norm_mobile = normalize_mobile(raw_mobile)

        # -------------------------------------------------------------
        # Rule 3: Both customer name and mobile number are empty -> Skip
        # -------------------------------------------------------------
        if not norm_name and not norm_mobile:
            logger.debug(
                "[CUSTOMER_SKIPPED_EMPTY_IDENTITY] upload_id=%s source_row=%s",
                upload_id, source_row_num
            )
            t_stat.skipped += 1
            continue

        # -------------------------------------------------------------
        # Rule 1: Mobile present -> mobile is the unique customer matching key.
        # -------------------------------------------------------------
        if norm_mobile:
            customer_match = mobile_to_customer.get(norm_mobile)
            if customer_match is not None:
                if "id" in customer_match:
                    target_id = customer_match["id"]
                    existing_name = customer_match.get("name")
                    if is_name_blank_or_null(existing_name) and norm_name:
                        pending_name_updates[target_id] = str(raw_name).strip()
                        customer_match["name"] = str(raw_name).strip()
                        record = customer_match.get("record")
                        if isinstance(record, dict):
                            record[customer_name_db_col] = str(raw_name).strip()

                    update_repeat_visit_date(target_id, row)
                    id_store.register_row_id(table_name, int(orig_idx), target_id)
                    id_store.register_id(table_name, norm_mobile, target_id)
                    if raw_mobile:
                        id_store.register_id(table_name, str(raw_mobile).strip(), target_id)
                    resolved_name = normalize_name(customer_match.get("name")) or norm_name
                    if resolved_name:
                        id_store.register_id(table_name, resolved_name, target_id)
                    id_store.register_composite_key(table_name, (norm_name, norm_mobile), target_id)
                    t_stat.reused += 1
                else:
                    p_idx = customer_match["pending_index"]
                    p_cust = pending_customers[p_idx]
                    p_cust["row_indices"].append(int(orig_idx))
                    if is_name_blank_or_null(p_cust["insert_name"]) and norm_name:
                        p_cust["insert_name"] = str(raw_name).strip()
                        p_cust["param_dict"][customer_name_db_col] = str(raw_name).strip()
                        p_cust["norm_name"] = norm_name
                    update_pending_repeat_visit_date(p_idx, row)
                    t_stat.reused += 1
            else:
                # No existing customer uses this mobile; create pending new customer
                insert_name = str(raw_name).strip() if norm_name else ""
                insert_mobile = str(raw_mobile).strip() if raw_mobile is not None and not is_name_blank_or_null(raw_mobile) else norm_mobile

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
                    outlet_id_cache=outlet_id_cache
                )

                set_insert_visit_dates(param_dict, row)
                p_idx = len(pending_customers)
                p_cust = {
                    "pending_id": p_idx,
                    "norm_mobile": norm_mobile,
                    "raw_mobile": raw_mobile,
                    "norm_name": norm_name,
                    "raw_name": raw_name,
                    "insert_name": insert_name,
                    "insert_mobile": insert_mobile,
                    "param_dict": param_dict,
                    "source_row_num": source_row_num,
                    "primary_row_idx": int(orig_idx),
                    "row_indices": [int(orig_idx)]
                }
                pending_customers.append(p_cust)
                mobile_to_customer[norm_mobile] = {"pending_index": p_idx}
                if norm_name:
                    name_mobile_to_customer[(norm_name, norm_mobile)] = {"pending_index": p_idx}

        # -------------------------------------------------------------
        # Rule 2: Mobile is empty, but customer name is present
        # -------------------------------------------------------------
        else:
            name_match = name_to_customer.get(norm_name)
            if name_match is not None:
                if "id" in name_match:
                    target_id = name_match["id"]
                    update_repeat_visit_date(target_id, row)
                    id_store.register_row_id(table_name, int(orig_idx), target_id)
                    id_store.register_id(table_name, norm_name, target_id)
                    id_store.register_composite_key(table_name, (norm_name, ""), target_id)
                    t_stat.reused += 1
                else:
                    p_idx = name_match["pending_index"]
                    p_cust = pending_customers[p_idx]
                    p_cust["row_indices"].append(int(orig_idx))
                    update_pending_repeat_visit_date(p_idx, row)
                    t_stat.reused += 1
            else:
                # Insert new name-only customer
                insert_name = str(raw_name).strip()
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
                    outlet_id_cache=outlet_id_cache
                )

                set_insert_visit_dates(param_dict, row)
                p_idx = len(pending_customers)
                p_cust = {
                    "pending_id": p_idx,
                    "norm_mobile": "",
                    "raw_mobile": None,
                    "norm_name": norm_name,
                    "raw_name": raw_name,
                    "insert_name": insert_name,
                    "insert_mobile": insert_mobile,
                    "param_dict": param_dict,
                    "source_row_num": source_row_num,
                    "primary_row_idx": int(orig_idx),
                    "row_indices": [int(orig_idx)]
                }
                pending_customers.append(p_cust)
                name_to_customer[norm_name] = {"pending_index": p_idx}

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
    # Pass 2: High-Performance Multi-Row Batch INSERT for New Customers
    # -------------------------------------------------------------
    def _register_inserted_customer(new_id: Any, p_cust: Dict[str, Any], was_updated: bool = False) -> None:
        if isinstance(new_id, float) and new_id.is_integer():
            new_id = int(new_id)
        elif str(new_id).endswith(".0"):
            new_id = int(float(new_id))

        generated_ids_by_table[table_name].append(new_id)
        source_row_indices_with_insert.add(p_cust["primary_row_idx"])
        for idx in p_cust["row_indices"]:
            id_store.register_row_id(table_name, idx, new_id)

        if p_cust["norm_mobile"]:
            id_store.register_id(table_name, p_cust["norm_mobile"], new_id)
            if p_cust["raw_mobile"]:
                id_store.register_id(table_name, str(p_cust["raw_mobile"]).strip(), new_id)
            if p_cust["norm_name"]:
                id_store.register_id(table_name, p_cust["norm_name"], new_id)
            id_store.register_composite_key(table_name, (p_cust["norm_name"], p_cust["norm_mobile"]), new_id)
            cust_rec = {"id": new_id, "name": p_cust["insert_name"], "mobile": p_cust["insert_mobile"], "record": p_cust["param_dict"]}
            mobile_to_customer[p_cust["norm_mobile"]] = cust_rec
            if p_cust["norm_name"]:
                name_mobile_to_customer[(p_cust["norm_name"], p_cust["norm_mobile"])] = cust_rec
        else:
            if p_cust["norm_name"]:
                id_store.register_id(table_name, p_cust["norm_name"], new_id)
                id_store.register_composite_key(table_name, (p_cust["norm_name"], ""), new_id)
                cust_rec = {"id": new_id, "name": p_cust["insert_name"], "mobile": None, "record": p_cust["param_dict"]}
                name_to_customer[p_cust["norm_name"]] = cust_rec

        if p_cust["pending_id"] in pending_new_cust_repeat_dates:
            pending_repeat_dates[new_id] = pending_new_cust_repeat_dates[p_cust["pending_id"]]

        if was_updated:
            t_stat.updated += 1
        else:
            t_stat.inserted += 1

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
                    p_cust = subchunk[0]
                    res_val = execute_customer_insert(
                        conn=conn,
                        dialect_name=dialect_name,
                        table_name=table_name,
                        param_dict=p_cust["param_dict"],
                        pk_col=pk_col,
                        t_meta=t_meta,
                        source_row_num=p_cust["source_row_num"]
                    )
                    new_id, was_upd = res_val if isinstance(res_val, tuple) else (res_val, False)
                    _register_inserted_customer(new_id, p_cust, was_updated=was_upd)
                    return

                val_clauses = []
                batch_params = {}
                for r_idx, p_cust in enumerate(subchunk):
                    row_placeholders = []
                    for c in cols:
                        param_name = f"{c}_{r_idx}"
                        row_placeholders.append(f":{param_name}")
                        batch_params[param_name] = _to_native_py(p_cust["param_dict"].get(c))
                    val_clauses.append(f"({', '.join(row_placeholders)})")

                if dialect_name == "mssql":
                    sql = (
                        f"SET NOCOUNT ON;\n"
                        f"DECLARE @ins_cust TABLE (_cid BIGINT, _rn INT IDENTITY(1,1));\n"
                        f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)})\n"
                        f"OUTPUT INSERTED.{quoted_pk} INTO @ins_cust (_cid)\n"
                        f"VALUES {', '.join(val_clauses)};\n"
                        f"SELECT _cid, _rn FROM @ins_cust ORDER BY _rn ASC;"
                    )
                elif dialect_name in ("postgresql", "postgres", "sqlite"):
                    sql = f"INSERT INTO {quoted_tbl} ({', '.join(quoted_cols)}) VALUES {', '.join(val_clauses)} RETURNING {quoted_pk}"
                else:
                    sql = None

                if sql:
                    try:
                        res = conn.execute(text(sql), batch_params)
                        fetched_rows = res.fetchall()
                        if len(fetched_rows) == len(subchunk):
                            for r_fetch, p_cust in zip(fetched_rows, subchunk):
                                new_id = r_fetch[0]
                                _register_inserted_customer(new_id, p_cust)
                            return
                    except Exception as batch_err:
                        err_msg = str(batch_err).lower()
                        is_dup_err = ("2627" in err_msg or "2601" in err_msg or "duplicate" in err_msg or "unique" in err_msg or "primary key" in err_msg)
                        if is_dup_err and len(subchunk) > 1:
                            try:
                                # High-performance batch duplicate resolution:
                                # Look up all matching mobile/name records in SQL Server in 1 single batch query
                                q_mob = quote_identifier(customer_mobile_db_col, dialect_name)
                                q_nm = quote_identifier(customer_name_db_col, dialect_name)
                                mob_map = {p["norm_mobile"]: p for p in subchunk if p.get("norm_mobile")}
                                name_map = {p["norm_name"]: p for p in subchunk if not p.get("norm_mobile") and p.get("norm_name")}

                                existing_matches: Dict[int, Any] = {}

                                if mob_map:
                                    m_items = list(mob_map.keys())
                                    ph_m = [f":m_{k}" for k in range(len(m_items))]
                                    p_m = {f"m_{k}": m for k, m in enumerate(m_items)}
                                    sel_m_sql = f"SELECT {quoted_pk}, {q_mob} FROM {quoted_tbl} WHERE {q_mob} IN ({', '.join(ph_m)})"
                                    m_res = conn.execute(text(sel_m_sql), p_m).fetchall()
                                    for row_m in m_res:
                                        found_cid = row_m[0]
                                        found_mob = normalize_mobile(row_m[1])
                                        if found_mob in mob_map:
                                            p_match = mob_map[found_mob]
                                            existing_matches[p_match["pending_id"]] = found_cid

                                if name_map:
                                    n_items = list(name_map.keys())
                                    ph_n = [f":n_{k}" for k in range(len(n_items))]
                                    p_n = {f"n_{k}": n for k, n in enumerate(n_items)}
                                    sel_n_sql = f"SELECT {quoted_pk}, {q_nm} FROM {quoted_tbl} WHERE ({q_mob} IS NULL OR LTRIM(RTRIM(CAST({q_mob} AS NVARCHAR(MAX)))) = '') AND {q_nm} IN ({', '.join(ph_n)})"
                                    n_res = conn.execute(text(sel_n_sql), p_n).fetchall()
                                    for row_n in n_res:
                                        found_cid = row_n[0]
                                        found_nm = normalize_name(row_n[1])
                                        if found_nm in name_map:
                                            p_match = name_map[found_nm]
                                            existing_matches[p_match["pending_id"]] = found_cid

                                existing_sub = []
                                new_sub = []
                                for p_c in subchunk:
                                    if p_c["pending_id"] in existing_matches:
                                        existing_sub.append((p_c, existing_matches[p_c["pending_id"]]))
                                    else:
                                        new_sub.append(p_c)

                                if existing_sub:
                                    upd_cols = [c for c in cols if c != pk_col]
                                    quoted_upd = [f"{quote_identifier(c, dialect_name)} = :{c}" for c in upd_cols]
                                    upd_stmt = text(f"UPDATE {quoted_tbl} SET {', '.join(quoted_upd)} WHERE {quoted_pk} = :_override_cid")
                                    batch_upd_params = []
                                    for p_c, exist_id in existing_sub:
                                        _register_inserted_customer(exist_id, p_c, was_updated=True)
                                        u_p = {c: _to_native_py(p_c["param_dict"].get(c)) for c in upd_cols}
                                        u_p["_override_cid"] = exist_id
                                        batch_upd_params.append(u_p)

                                    if batch_upd_params:
                                        conn.execute(upd_stmt, batch_upd_params)

                                if new_sub:
                                    if len(new_sub) < len(subchunk):
                                        _insert_cust_subchunk(new_sub)
                                    else:
                                        mid = len(subchunk) // 2
                                        _insert_cust_subchunk(subchunk[:mid])
                                        _insert_cust_subchunk(subchunk[mid:])
                                return
                            except Exception as res_batch_err:
                                logger.debug(f"[CUSTOMER_BATCH_DUP_FALLBACK] {res_batch_err}")
                                mid = len(subchunk) // 2
                                _insert_cust_subchunk(subchunk[:mid])
                                _insert_cust_subchunk(subchunk[mid:])
                                return

                        # Fallback bisection for non-duplicate errors
                        if len(subchunk) > 1 and is_dup_err:
                            mid = len(subchunk) // 2
                            _insert_cust_subchunk(subchunk[:mid])
                            _insert_cust_subchunk(subchunk[mid:])
                            return

                for p_cust in subchunk:
                    res_val = execute_customer_insert(
                        conn=conn,
                        dialect_name=dialect_name,
                        table_name=table_name,
                        param_dict=p_cust["param_dict"],
                        pk_col=pk_col,
                        t_meta=t_meta,
                        source_row_num=p_cust["source_row_num"]
                    )
                    new_id, was_upd = res_val if isinstance(res_val, tuple) else (res_val, False)
                    _register_inserted_customer(new_id, p_cust, was_updated=was_upd)

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
    # Pass 3: Flush Pending Name Updates & Batched Repeat Visit Dates
    # -------------------------------------------------------------
    if pending_name_updates:
        quoted_name_col = quote_identifier(customer_name_db_col, dialect_name)
        quoted_pk_local = quote_identifier(pk_col, dialect_name)
        upd_stmt = text(
            f"UPDATE {quoted_tbl} SET {quoted_name_col} = :customer_name "
            f"WHERE {quoted_pk_local} = :customer_id"
        )
        upd_params = [
            {"customer_name": name, "customer_id": cid}
            for cid, name in pending_name_updates.items()
        ]
        conn.execute(upd_stmt, upd_params)
        logger.info("[CUSTOMER_NAME_BATCH_UPDATED] count=%s", len(upd_params))

    flush_repeat_visit_dates()

    logger.info(
        f"[CUSTOMER_MIGRATION] Finished '{table_name}': "
        f"inserted={t_stat.inserted}, reused={t_stat.reused}, "
        f"updated={t_stat.updated}, skipped={t_stat.skipped}"
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
    outlet_id_cache: Optional[Dict[str, Any]] = None
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
            from app.migration.lookup_resolver import get_single_outlet_id
            cached_outlet_id = outlet_id_cache.get(parent_tbl) if outlet_id_cache else None
            if cached_outlet_id is None:
                cached_outlet_id = get_single_outlet_id(parent_records_by_table, parent_tbl, parent_col)
                if outlet_id_cache is not None:
                    outlet_id_cache[parent_tbl] = cached_outlet_id
            outlet_col_meta = col_meta_map.get(fk_col)
            param_dict[fk_col] = sanitize_value(
                cached_outlet_id,
                getattr(outlet_col_meta, "type", ""),
                getattr(outlet_col_meta, "nullable", False)
            )
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
    customer_mobile_db_col = next(
        (c.name for c in t_meta.columns if any(k in c.name.lower() for k in (
            "mobile_number", "mobile", "phone_number", "customer_phone",
            "customer_mobile", "contact_number", "phone"
        ))),
        "Mobile_Number"
    )

    db_to_excel = {m.database_column: m.excel_column for m in mappings if m.excel_column in df.columns}
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

    known_mobiles: Set[str] = set()
    known_names_no_mobile: Set[str] = set()
    known_name_mobile_pairs: Set[Tuple[str, str]] = set()

    ins_cnt = 0
    skip_cnt = 0
    reus_cnt = 0

    df_records = df.to_dict(orient="records")
    for row in df_records:
        raw_n = get_df_val(row, name_ex_col)
        raw_m = get_df_val(row, mobile_ex_col)
        n_n = normalize_name(raw_n)
        n_m = normalize_mobile(raw_m)

        # Rule 3: Both empty -> Skip
        if not n_n and not n_m:
            skip_cnt += 1
            continue

        # Both present -> match only the name+mobile pair. Empty name + mobile
        # -> match by mobile alone.
        if n_m:
            if (n_n and (n_n, n_m) in known_name_mobile_pairs) or (not n_n and n_m in known_mobiles):
                reus_cnt += 1
            else:
                ins_cnt += 1
                known_mobiles.add(n_m)
                if n_n:
                    known_name_mobile_pairs.add((n_n, n_m))

        # Rule 2: Mobile empty, name present
        else:
            if n_n in known_names_no_mobile:
                reus_cnt += 1
            else:
                ins_cnt += 1
                known_names_no_mobile.add(n_n)

    return ins_cnt, skip_cnt, reus_cnt

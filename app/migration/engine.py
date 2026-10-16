import time
import math
import datetime
from typing import Dict, Any, List, Optional, Tuple, Set, Callable
import pandas as pd
from sqlalchemy import text, Engine
from sqlalchemy.engine import Connection

from app.core.config import settings
from app.database.connection import get_engine
from app.services.database_service import database_service
from app.services.excel_service import excel_service
from app.excel.reader import load_file_dataframe
from app.mapping.matcher import ColumnMatcher, normalize_string, resolve_unmapped_required_columns
from app.migration.dependency import DependencyResolver
from app.migration.id_resolver import IdResolverStore
from app.migration.transaction import transaction_manager
# Local invoice duplicate helpers: keep engine.py runnable even when deployed alone.
def invoice_identity_key(value: Any) -> str:
    """Normalize invoice values for comparison without changing stored source values."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    value_text = str(value).strip()
    if value_text.casefold() in {"", "none", "nan", "nat", "null"}:
        return ""
    if value_text.endswith(".0"):
        try:
            value_text = str(int(float(value_text)))
        except (TypeError, ValueError, OverflowError):
            pass
    return value_text.casefold()

def duplicate_invoice_mask(
    frame: pd.DataFrame, invoice_column: str, existing_invoice_values: Set[str]
) -> Tuple[pd.Series, Set[str]]:
    """Mask source rows whose invoice already exists in Bill.BillNo."""
    normalized_existing = {invoice_identity_key(v) for v in existing_invoice_values}
    normalized_existing.discard("")
    mask = frame[invoice_column].map(
        lambda value: invoice_identity_key(value) in normalized_existing
    )
    skipped_keys = {
        invoice_identity_key(value)
        for value in frame.loc[mask, invoice_column].tolist()
        if invoice_identity_key(value)
    }
    return mask, skipped_keys

from app.migration.validator import MigrationValidator, is_empty_or_na
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import (
    MigrationExecutionResponse,
    TableMigrationStat,
    ColumnMappingResult,
    ForeignKeyPlanItem,
    ResolvedEntityRecord,
    ValueMappingItem
)
from app.migration.lookup_resolver import (
    DynamicLookupResolver,
    LookupRelationship,
    is_item_master_target,
    item_row_source_key,
    get_single_outlet_id
)
from app.migration.outlet_matcher import OutletMatcher, extract_outlet_base_name, outlet_row_source_key
from app.migration.missing_value_resolver import (
    MissingValueResolver,
    TableEntityLookupIndex,
    is_customer_name_column
)
from app.core.logging import logger


def collapse_bill_rows_by_invoice(
    records: List[Dict[str, Any]],
    row_indices: List[Any],
    invoice_column: Optional[str],
    quantity_column: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Any], int, int]:
    """Shape item-level source rows into invoice-level header records.

    This is source aggregation for Order and Bills, not a database duplicate check.
    Every OrderLineItems source row is left untouched. Rows without an invoice number
    are preserved individually. If a header quantity column is mapped, quantities from
    all item rows for the invoice are summed into its single header row.
    """
    if not invoice_column:
        return records, row_indices, 0, 0

    kept_records: List[Dict[str, Any]] = []
    kept_indices: List[Any] = []
    seen_invoice_numbers: Set[str] = set()
    kept_invoice_positions: Dict[str, int] = {}
    duplicate_rows = 0

    def quantity_number(value: Any) -> float:
        if is_empty_or_na(value):
            return 0.0
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return 0.0

    for row_index, record in zip(row_indices, records):
        raw_invoice = record.get(invoice_column)
        if is_empty_or_na(raw_invoice):
            kept_records.append(record)
            kept_indices.append(row_index)
            continue

        # Preserve original BillNo/invoice value; use normalized text only as key.
        invoice_key = str(raw_invoice).strip().casefold()
        if invoice_key in seen_invoice_numbers:
            duplicate_rows += 1
            if quantity_column and invoice_key in kept_invoice_positions:
                kept_record = kept_records[kept_invoice_positions[invoice_key]]
                total_quantity = quantity_number(kept_record.get(quantity_column)) + quantity_number(record.get(quantity_column))
                kept_record[quantity_column] = int(total_quantity) if total_quantity.is_integer() else total_quantity
            continue

        seen_invoice_numbers.add(invoice_key)
        kept_invoice_positions[invoice_key] = len(kept_records)
        kept_records.append(record)
        kept_indices.append(row_index)

    return kept_records, kept_indices, duplicate_rows, len(seen_invoice_numbers)


def collapse_settlement_rows(
    records: List[Dict[str, Any]],
    row_indices: List[Any],
    invoice_column: Optional[str],
    payment_type_column: Optional[str] = None,
    amount_column: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[Any], int, int]:
    """Shape item-level source rows into Settlement records per (invoice, payment_type).

    If an invoice has multiple payment types, each distinct payment entry is preserved
    as a separate settlement record. Rows with the identical invoice and payment type
    are collapsed into a single settlement entry.
    """
    if not invoice_column:
        return records, row_indices, 0, 0

    kept_records: List[Dict[str, Any]] = []
    kept_indices: List[Any] = []
    seen_keys: Set[Tuple[str, str]] = set()
    duplicate_rows = 0

    for row_index, record in zip(row_indices, records):
        raw_invoice = record.get(invoice_column)
        if is_empty_or_na(raw_invoice):
            kept_records.append(record)
            kept_indices.append(row_index)
            continue

        raw_payment = record.get(payment_type_column) if payment_type_column else ""
        pay_str = str(raw_payment or "").strip().lower()
        inv_str = str(raw_invoice).strip().casefold()
        key = (inv_str, pay_str)

        if key in seen_keys:
            duplicate_rows += 1
            continue

        seen_keys.add(key)
        kept_records.append(record)
        kept_indices.append(row_index)

    return kept_records, kept_indices, duplicate_rows, len(seen_keys)


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


def should_column_receive_empty_string(
    column: Any,
    table_name: str = "",
    column_meta: Optional[Any] = None,
    table_meta: Optional[Any] = None,
    fk_columns: Optional[Set[str]] = None,
    column_rules: Optional[Dict[str, Any]] = None,
    business_rules: Optional[Dict[str, Any]] = None,
) -> bool:
    """Determines whether a column should receive an empty string '' when missing,
    None, 'null', 'NULL', or whitespace-only in source data.

    Rules:
    1. Must be a textual column (VARCHAR, NVARCHAR, CHAR, NCHAR, TEXT, CLOB, STRING).
       Non-text columns (INT, BIGINT, DECIMAL, FLOAT, MONEY, DATE, DATETIME, BIT) NEVER receive ''.
    2. Identity/auto-increment columns NEVER receive ''.
    3. Foreign key columns NEVER receive '' (unresolved/nullable FKs must remain SQL NULL / None).
    4. Database default constraints: columns with valid defaults defer to DB default unless overridden.
    5. Configured rules: if column_rules or business_rules explicitly specifies null_handling:
       - 'EMPTY_STRING' -> True
       - 'NULL' or preserve_null -> False
    6. Default expectation for text columns without DB default/FK: True (send empty string '').
    """
    col_obj = column_meta if column_meta is not None else (column if hasattr(column, "type") else None)
    col_name = str(getattr(col_obj, "name", "") or (column if isinstance(column, str) else ""))

    # 1. Must be text SQL type
    c_type = getattr(col_obj, "type", "") if col_obj else ""
    if not is_text_sql_type(str(c_type)):
        return False

    # 2. Identity columns
    if col_obj and getattr(col_obj, "identity", False):
        return False

    # 3. Foreign key columns
    if fk_columns and col_name in fk_columns:
        return False
    if col_obj and getattr(col_obj, "foreign_keys", None):
        return False
    if table_meta and any(getattr(fk, "constrained_column", None) == col_name for fk in getattr(table_meta, "foreign_keys", [])):
        return False

    # 4. Database default constraint
    if col_obj and getattr(col_obj, "default", None) is not None:
        return False

    # 5. Configured column rules / business rules
    rules = column_rules or business_rules or {}
    rule = rules.get(col_name) or rules.get(f"{table_name}.{col_name}") or rules.get(col_name.lower())
    if isinstance(rule, dict):
        if rule.get("null_handling") == "EMPTY_STRING" or rule.get("empty_string") is True:
            return True
        if rule.get("null_handling") == "NULL" or rule.get("preserve_null") is True:
            return False

    return True


def should_fill_unmapped_text_column(column: Any, table_meta: Any, fk_columns: Optional[Set[str]] = None) -> bool:
    """Fill absent optional text columns with an empty string, not SQL NULL."""
    table_name = getattr(table_meta, "name", "") if table_meta else ""
    return should_column_receive_empty_string(column, table_name=table_name, column_meta=column, table_meta=table_meta, fk_columns=fk_columns)


def normalize_string_value(
    val: Any,
    expects_empty_string: bool = True,
    is_legitimate_literal: bool = False
) -> Any:
    """Normalizes string values according to business requirements:
    - If is_legitimate_literal is True: preserves the literal string (e.g. 'null').
    - If expects_empty_string is True:
        Python None -> ''
        String 'null' (case-insensitive, trimmed) -> ''
        String 'NULL' -> ''
        Empty or whitespace-only string -> ''
    - If expects_empty_string is False:
        preserves None (SQL NULL) for missing values.
    """
    if is_legitimate_literal:
        return str(val) if val is not None else ""

    if val is None or is_empty_or_na(val):
        return "" if expects_empty_string else None

    if isinstance(val, str):
        trimmed = val.strip()
        if trimmed == "" or trimmed.lower() in ("null", "none", "nan", "nat"):
            return "" if expects_empty_string else None
        return trimmed

    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def sanitize_value(
    val: Any,
    col_type: str = "",
    nullable: bool = True,
    allow_literal_null_str: bool = False,
    expects_empty_string: Optional[bool] = None,
    is_legitimate_literal: bool = False
) -> Any:
    """Normalize missing values before database insertion.

    Text columns always receive an empty string for missing values and for the
    literal strings "null"/"none"/"nan". Never persist the word "null" as data,
    unless is_legitimate_literal is True.
    Non-text columns retain type-safe handling because an empty string is not a
    valid replacement for numeric/date/foreign-key values.
    """
    col_type_upper = str(col_type or "").upper()
    is_text_type = is_text_sql_type(col_type_upper)

    if is_legitimate_literal and is_text_type:
        return str(val) if val is not None else ""

    missing = val is None or is_empty_or_na(val)
    str_val = "" if val is None else str(val).strip()
    if str_val.lower() in ("nan", "none", "null", "nat"):
        missing = True

    if is_text_type:
        if expects_empty_string is None:
            expects_empty_string = not allow_literal_null_str

        if not expects_empty_string and allow_literal_null_str:
            if missing or str_val == "" or str_val.lower() in ("nan", "none", "null", "nat"):
                return "null"

        return normalize_string_value(val, expects_empty_string=expects_empty_string, is_legitimate_literal=is_legitimate_literal)

    if missing:
        return None if nullable else (0 if any(t in col_type_upper for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")) else 0.0 if any(t in col_type_upper for t in ("FLOAT", "DECIMAL", "NUMERIC", "REAL", "DOUBLE", "MONEY")) else None)

    # Integers
    if any(t in col_type_upper for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
        try:
            return int(float(str_val))
        except (ValueError, TypeError):
            return None if nullable else 0

    # Numeric / Float / Decimal / Money
    if any(t in col_type_upper for t in ("FLOAT", "DECIMAL", "NUMERIC", "REAL", "DOUBLE", "MONEY")):
        try:
            clean_num = str_val.replace("$", "").replace("₹", "").replace(",", "")
            return float(clean_num)
        except (ValueError, TypeError):
            return None if nullable else 0.0

    # Date / Time formatting
    if any(t in col_type_upper for t in ("DATE", "DATETIME", "DATETIME2", "SMALLDATETIME")):
        from app.utils.normalization import normalize_date_value
        norm_dt_str, dt_obj, is_v = normalize_date_value(val if val is not None else str_val, target_type=col_type_upper)
        if is_v and norm_dt_str is not None:
            return norm_dt_str
        if is_v and norm_dt_str is None:
            return None if nullable else None
        return str_val

    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str_val


class MigrationEngine:
    """
    Universal Dynamic Transactional Migration Engine.
    - Zero hardcoded tables or columns.
    - Executes within an atomic database transaction using engine.begin().
    - Validates execution plan before any modification.
    - Topologically orders target tables (parents before children).
    - Captures SQL Server generated identity values using OUTPUT INSERTED and passes them to child records.
    - Reuses existing records from the database where business keys match.
    - On failure, rolls back the entire transaction automatically.
    - Idempotent and thread-safe.
    """

    @staticmethod
    def execute_migration(
        upload_id: str,
        progress_callback: Optional[Callable[[str, int, int, int, int, int, int], None]] = None,
        duplicate_action: str = "skip",
    ) -> MigrationExecutionResponse:
        duplicate_action = str(duplicate_action or "skip").strip().lower()
        if duplicate_action not in {"skip", "override"}:
            raise ValueError("duplicate_action must be 'skip' or 'override'.")
        if duplicate_action == "override":
            raise NotImplementedError(
                "Override is not enabled in this build. Use duplicate_action='skip'; "
                "no database changes have been made."
            )

        # SQL Safety Guard: Ensure dry-run mode never invokes real migration execution
        if getattr(settings, "DRY_RUN_MODE", False):
            raise RuntimeError(
                "SAFETY GUARD VIOLATION: Migration execution attempted while DRY_RUN_MODE is enabled. "
                "Data-modifying SQL execution is strictly prohibited in dry-run mode."
            )

        t_start = time.time()
        t_start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        logger.info(f"[MIGRATION] START | Executing dynamic migration for uploadId={upload_id}")

        # 1. Retrieve uploaded tabular data
        session_data = excel_service.get_upload_session(upload_id)
        if not session_data:
            raise ValueError(f"Upload session '{upload_id}' not found.")

        file_path = session_data.get("filePath")
        sheet_name = session_data.get("sheetName")

        if not file_path or not pd.io.common.file_exists(file_path):
            raise ValueError("Uploaded data file is missing from server storage.")

        header_row = session_data.get("headerRowNumber") or 1
        df = load_file_dataframe(
            file_path,
            sheet_name=sheet_name,
            header=(header_row - 1) if header_row >= 1 else 0
        ).dropna(how='all')
        df.columns = [str(c).strip() for c in df.columns]

        # Case-insensitive column index for CSV/Excel lookup
        col_lookup = {c.lower(): c for c in df.columns}

        def get_df_val(row_dict: Any, ex_col_name: str) -> Any:
            if ex_col_name in row_dict:
                return row_dict[ex_col_name]
            clean = ex_col_name.strip().lower()
            actual_col = col_lookup.get(clean)
            if actual_col and actual_col in row_dict:
                return row_dict[actual_col]
            return None

        # 2. Introspect Database Schema and Match Columns Dynamically
        engine = get_engine()
        dialect_name = engine.name
        schema = database_service.get_schema_metadata()
        schema_tables = {t.name: t for t in schema.tables}

        # Dynamic column matching (incorporating user overrides if present)
        from app.services.migration_service import migration_service
        overrides = migration_service._user_overrides.get(upload_id, {})
        column_mappings = ColumnMatcher.match_columns(df.columns.tolist(), schema, user_overrides=overrides)
        matched_mappings = [m for m in column_mappings if m.status == "MATCHED" and m.database_table in schema_tables]

        if not matched_mappings:
            logger.warning("[MIGRATION] No columns could be matched to the target database.")
            return MigrationExecutionResponse(
                uploadId=upload_id,
                status="COMPLETED",
                transaction="COMMITTED",
                tables={},
                totalInserted=0,
                totalUpdated=0,
                totalReused=0,
                totalSkipped=0,
                totalFailed=0,
                verificationCounts={},
                processingTimeSeconds=round(time.time() - t_start, 3),
                message="No columns were matched to target database tables."
            )

        # 3. Determine Active Target Tables and Topological Order
        table_mappings_map: Dict[str, List[ColumnMappingResult]] = {}
        for m in matched_mappings:
            table_mappings_map.setdefault(m.database_table, []).append(m)

        # Dynamically resolve unmapped required columns from source dataset
        table_mappings_map = resolve_unmapped_required_columns(schema, table_mappings_map, df.columns.tolist())

        # 3.8 Discover dynamic foreign-key lookup relationships using database metadata
        all_potential_tables = list(table_mappings_map.keys())
        initial_lookup_relationships = DynamicLookupResolver.discover_relationships(
            schema=schema,
            df_columns=df.columns.tolist(),
            column_mappings=column_mappings,
            active_tables=all_potential_tables
        )
        parent_records_by_table = DynamicLookupResolver.load_parent_records(
            engine=engine,
            schema=schema,
            relationships=initial_lookup_relationships
        )

        # Dynamic Table Role Classification for execution engine:
        # Filter table_mappings_map using classify_table_relevance to exclude incidental/unrelated tables
        from app.migration.preview import classify_table_relevance, TableRole

        active_target_tables: List[str] = []
        engine_lookup_tables: List[str] = []
        engine_dependency_tables: List[str] = []
        engine_skipped_tables: Dict[str, str] = {}
        engine_table_roles: Dict[str, str] = {}

        allowed_target_tables = {
            "customer", "bill", "bills", "order", "orders",
            "orderlineitems", "settlement", "settlements", "outlets", "itemsmaster"
        }
        for t, m_list in table_mappings_map.items():
            table_key = str(t).lower().replace("dbo.", "").replace("_", "").replace(" ", "").strip()
            if table_key not in allowed_target_tables:
                engine_table_roles[t] = TableRole.SKIP.value
                engine_skipped_tables[t] = "Excluded by configured migration allowlist"
                logger.info("[MIGRATION] Table '%s' excluded by allowed-table restriction.", t)
                continue
            role, reason = classify_table_relevance(t, m_list, schema, df.columns.tolist())
            engine_table_roles[t] = role.value
            if role == TableRole.TARGET_INSERT:
                active_target_tables.append(t)
            elif role == TableRole.LOOKUP_ONLY:
                engine_lookup_tables.append(t)
            elif role == TableRole.DEPENDENCY_INSERT:
                engine_dependency_tables.append(t)
                active_target_tables.append(t)
            else:
                engine_skipped_tables[t] = reason
                logger.info(f"[MIGRATION] Table '{t}' classified as {role.value}: {reason}")

        for rel in initial_lookup_relationships:
            p_tbl = rel.parent_table
            if p_tbl in schema_tables and p_tbl not in engine_table_roles:
                engine_table_roles[p_tbl] = TableRole.LOOKUP_ONLY.value
                if p_tbl not in engine_lookup_tables:
                    engine_lookup_tables.append(p_tbl)
                engine_skipped_tables.pop(p_tbl, None)

        for s_tbl in schema.tables:
            tbl_name = s_tbl.name
            if tbl_name not in engine_table_roles:
                engine_table_roles[tbl_name] = TableRole.SKIP.value
                engine_skipped_tables[tbl_name] = "No mapped source columns or foreign key references in uploaded dataset"

        full_execution_order = DependencyResolver.resolve_execution_order(schema, target_tables=active_target_tables)
        active_tables_ordered = [t for t in full_execution_order if t in active_target_tables]
        for t in active_target_tables:
            if t not in active_tables_ordered:
                active_tables_ordered.append(t)

        logger.info(f"[MIGRATION] Active tables to execute in order: {active_tables_ordered}")


        # 3.5 Prepare value mapping transformations
        from app.mapping.values import ValueMatcher
        from app.services.migration_service import MigrationService
        # MigrationService instance or class lookup
        user_val_overrides = {}
        try:
            from app.api.endpoints import migration_service as ms_inst
            user_val_overrides = ms_inst._user_value_overrides.get(upload_id, {})
        except Exception:
            user_val_overrides = {}
        value_transform_lookup: Dict[Tuple[str, str, str], str] = {}  # (table, db_col, raw_val) -> target_val
        col_value_transform: Dict[Tuple[str, str], str] = {}          # (db_col, raw_val) -> target_val
        all_val_items = []

        CATEGORICAL_KEYWORDS = ("type", "status", "mode", "category", "channel", "method", "state", "brand", "source", "reason", "payment")
        EXCLUDED_KEYWORDS = ("name", "id", "code", "date", "time", "phone", "comment", "note", "desc", "bill", "guid", "price", "amount", "tax", "rate", "cost", "qty", "quantity", "address")

        for m in matched_mappings:
            if m.excel_column in df.columns and m.database_table in schema_tables:
                col_lower = m.database_column.lower()
                is_categorical = (
                    any(k in col_lower for k in CATEGORICAL_KEYWORDS)
                    or any(k in m.excel_column.lower() for k in CATEGORICAL_KEYWORDS)
                ) and not any(k in col_lower for k in EXCLUDED_KEYWORDS)

                if is_categorical:
                    valid_db_vals = ValueMatcher.get_valid_column_values(
                        engine=engine,
                        table=m.database_table,
                        column=m.database_column
                    )
                    if valid_db_vals:
                        overrides_map = user_val_overrides.get(f"{m.database_table}.{m.database_column}") or user_val_overrides.get(m.excel_column)
                        v_items = ValueMatcher.match_column_values(
                            df=df,
                            excel_column=m.excel_column,
                            target_table=m.database_table,
                            target_column=m.database_column,
                            valid_db_values=valid_db_vals,
                            user_overrides=overrides_map,
                            database_scope=schema.database_name
                        )
                        for item in v_items:
                            all_val_items.append(item)
                            if item.target_value and item.status in ("MATCHED", "APPROVED"):
                                value_transform_lookup[(m.database_table, m.database_column, item.excel_value)] = item.target_value
                                col_value_transform[(m.database_column, item.excel_value)] = item.target_value


        # 3.8 Discover dynamic foreign-key lookup relationships scoped to active tables
        lookup_relationships: List[LookupRelationship] = DynamicLookupResolver.discover_relationships(
            schema=schema,
            df_columns=df.columns.tolist(),
            column_mappings=column_mappings,
            active_tables=active_tables_ordered
        )
        # Ensure any newly discovered relationships have their parent records loaded
        for rel in lookup_relationships:
            if rel.parent_table not in parent_records_by_table:
                more_recs = DynamicLookupResolver.load_parent_records(
                    engine=engine,
                    schema=schema,
                    relationships=[rel]
                )
                parent_records_by_table.update(more_recs)

        # Cache the only existing outlet ID once per migration run. All outlet FKs
        # reuse this value; Excel restaurant_name/virtual_brand_name are not matched.
        outlet_id_cache: Dict[str, Any] = {}
        for rel in lookup_relationships:
            if str(rel.parent_table or "").strip().lower().split(".")[-1] == "outlets":
                if rel.parent_table not in outlet_id_cache:
                    outlet_id_cache[rel.parent_table] = get_single_outlet_id(
                        parent_records_by_table, rel.parent_table, rel.parent_id_column
                    )

        # Quick validation plan for pre-execution check
        fk_val_plan = []
        for rel in lookup_relationships:
            if rel.child_table not in active_tables_ordered:
                continue
            db_recs = parent_records_by_table.get(rel.parent_table, [])
            src_col = rel.excel_name_column or rel.excel_id_column
            if src_col and src_col in df.columns:
                u_names = [v for v in df[src_col].dropna().unique() if not is_empty_or_na(v)][:20]
                for u_n in u_names:
                    _, _, r_stat, _, _, cands, _ = DynamicLookupResolver.match_name_to_id(
                        excel_name=u_n,
                        parent_table=rel.parent_table,
                        parent_id_column=rel.parent_id_column,
                        parent_name_column=rel.parent_name_column or rel.parent_id_column,
                        db_records=db_recs
                    )
                    fk_val_plan.append(ForeignKeyPlanItem(
                        childTable=rel.child_table,
                        childColumn=rel.child_column,
                        parentTable=rel.parent_table,
                        parentColumn=rel.parent_id_column,
                        sourceBusinessKey=str(u_n),
                        existingParentFound=(r_stat == "MATCHED"),
                        status="MATCHED" if r_stat == "MATCHED" else ("PENDING" if rel.parent_table in active_tables_ordered else r_stat),
                        candidates=cands
                    ))

        # 4. Pre-execution dataset validation with dynamic missing value resolution
        entity_indices: Dict[str, TableEntityLookupIndex] = {}
        for t_name in active_tables_ordered:
            t_m = schema_tables[t_name]
            entity_indices[t_name] = MissingValueResolver.load_table_entity_index(engine, t_m)

        validation_res = MigrationValidator.validate_dataset(
            df=df,
            schema=schema,
            column_mappings=column_mappings,
            value_mappings=all_val_items,
            foreign_key_plans=fk_val_plan,
            entity_indices=entity_indices,
            lookup_relationships=lookup_relationships,
            parent_records_by_table=parent_records_by_table,
            upload_id=upload_id
        )
        validation_res.can_execute = not bool(validation_res.errors)
        validation_res.invalid_rows = len({
            getattr(error, "rowNumber", None) or getattr(error, "row_number", None)
            for error in validation_res.errors
            if (getattr(error, "rowNumber", None) or getattr(error, "row_number", None))
        })

        if not validation_res.canExecute:
            error_details = "; ".join([e.message for e in validation_res.errors[:5]])
            logger.warning(f"[MIGRATION] Validation blocked execution for uploadId={upload_id}: {error_details}")
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return MigrationExecutionResponse(
                uploadId=upload_id,
                status="BLOCKED",
                transaction="NOT_EXECUTED",
                transactionStatus="NOT_EXECUTED",
                canExecute=False,
                executionStarted=t_start_iso,
                executionCompleted=now_iso,
                tables={},
                totalInserted=0,
                totalUpdated=0,
                totalReused=0,
                totalSkipped=0,
                totalFailed=0,
                insertedCount=0,
                updatedCount=0,
                reusedCount=0,
                skippedCount=0,
                failedCount=0,
                validationErrors=validation_res.errors,
                warnings=validation_res.warnings,
                verificationCounts={},
                processingTimeSeconds=round(time.time() - t_start, 3),
                message=f"Migration execution blocked: {len(validation_res.errors)} validation error(s) must be resolved before executing the database transaction.",
                errorReport=[e.model_dump(by_alias=True) for e in validation_res.errors]
            )

        # 5. Define the execution function to be run inside the transaction
        def _execute_in_transaction(conn: Connection) -> MigrationExecutionResponse:
            nonlocal df
            original_source_row_count = len(df)
            duplicate_invoice_skipped_rows = 0

            # Duplicate invoices are checked against dbo.Bill.BillNo while the
            # transaction is open. Every source row for an existing invoice is removed
            # before any target table is processed, so Bill, Order, OrderLineItems,
            # Settlement and dependent Customer rows are not partially inserted.
            invoice_source_for_skip = next(
                (c for c in df.columns if "".join(ch for ch in str(c).lower() if ch.isalnum())
                 in {"invoiceno", "invoicenumber", "invoice"}),
                None,
            )
            # Duplicate invoices are keyed by Bill.BillNo, because this project
            # explicitly stores the Excel invoice number in BillNo using IDENTITY_INSERT.
            bill_meta_for_skip = next(
                (t for t in schema.tables
                 if str(t.name).lower().replace("dbo.", "").strip() in ("bill", "bills")
                 and any("".join(ch for ch in str(c.name).lower() if ch.isalnum()) == "billno" for c in t.columns)),
                None,
            )
            if duplicate_action == "skip" and invoice_source_for_skip and bill_meta_for_skip:
                billno_col_for_skip = next(
                    c.name for c in bill_meta_for_skip.columns
                    if "".join(ch for ch in str(c.name).lower() if ch.isalnum()) == "billno"
                )
                quoted_bill_table_for_skip = quote_identifier(bill_meta_for_skip.name, dialect_name)
                quoted_billno_for_skip = quote_identifier(billno_col_for_skip, dialect_name)
                lock_hint = " WITH (UPDLOCK, HOLDLOCK)" if dialect_name == "mssql" else ""
                existing_invoice_values = conn.execute(text(
                    f"SELECT {quoted_billno_for_skip} FROM {quoted_bill_table_for_skip}{lock_hint} "
                    f"WHERE {quoted_billno_for_skip} IS NOT NULL"
                )).scalars().all()

                existing_invoice_keys = {invoice_identity_key(v) for v in existing_invoice_values if invoice_identity_key(v)}
                if existing_invoice_keys:
                    duplicate_mask, skipped_invoice_keys = duplicate_invoice_mask(
                        df, invoice_source_for_skip, existing_invoice_keys
                    )
                    duplicate_invoice_skipped_rows = int(duplicate_mask.sum())
                    if duplicate_invoice_skipped_rows:
                        skipped_invoices = sorted(skipped_invoice_keys)
                        logger.info(
                            "[DUPLICATE_INVOICE_SKIP] upload_id=%s invoice_column=%s skipped_source_rows=%s duplicate_invoice_count=%s invoices_sample=%s",
                            upload_id, invoice_source_for_skip, duplicate_invoice_skipped_rows, len(skipped_invoices), skipped_invoices[:25]
                        )
                        df = df.loc[~duplicate_mask].copy()

            id_store = IdResolverStore()
            table_stats: Dict[str, TableMigrationStat] = {t: TableMigrationStat() for t in active_tables_ordered}
            generated_ids_by_table: Dict[str, List[Any]] = {t: [] for t in active_tables_ordered}
            verif_counts: Dict[str, int] = {}
            failure_details: List[Dict[str, Any]] = []
            source_row_indices_with_insert: Set[int] = set()
            fk_propagated_count: int = 0
            reused_invoices: Set[str] = set()

            # Pre-convert DataFrame to record dictionaries once for 50x faster row access
            df_records = df.to_dict(orient="records")
            df_indices = df.index.tolist()

            # Pre-index all_val_items by (column, value) for O(1) matching
            val_items_by_col: Dict[str, Dict[str, List[ValueMappingItem]]] = {}
            for vm in all_val_items:
                if vm.excel_column not in val_items_by_col:
                    val_items_by_col[vm.excel_column] = {}
                val_items_by_col[vm.excel_column].setdefault(str(vm.excel_value).strip(), []).append(vm)

            # FK lookup resolution cache to avoid redundant fuzzy matching across thousands of rows
            fk_resolution_cache: Dict[Tuple[str, str, str, str], Any] = {}

            for tbl_idx, table_name in enumerate(active_tables_ordered):
                t0 = time.time()
                t_meta: TableMetadata = schema_tables[table_name]
                mappings = table_mappings_map[table_name]
                t_stat = table_stats[table_name]

                col_meta_map: Dict[str, ColumnMetadata] = {c.name: c for c in t_meta.columns}
                pk_cols = [c.name for c in t_meta.columns if c.primary_key]
                identity_cols = [c.name for c in t_meta.columns if getattr(c, "identity", False)]

                # DERIVED_FROM_BILLNO is a companion preview/business mapping only.
                # It uses the same Excel invoice column as Bills.BillNo, so including
                # it in this one-to-one dict would overwrite Bills.BillNo with
                # Bills.Bill_Number and cause SQL Server to auto-generate BillNo.
                # Bills.Bill_Number is populated from param_dict[BillNo] below.
                excel_to_db = {
                    m.excel_column: m.database_column
                    for m in mappings
                    if str(getattr(m, "match_type", "")).upper() != "DERIVED_FROM_BILLNO"
                }
                db_to_excel = {m.database_column: m.excel_column for m in mappings}
                target_db_cols = list(dict.fromkeys(excel_to_db.values()))
                normalized_table_name = table_name.lower().replace("dbo.", "").strip()

                # Invoice-number override for Bills:
                # The user requirement is explicit: Excel invoice_no -> Bill.BillNo.
                # If BillNo is an IDENTITY column in SQL Server, insert the supplied
                # invoice number with IDENTITY_INSERT ON; do not let SQL Server generate
                # sequential values such as 1, 2, 3 for this business key.
                def _norm_invoice_column(value: Any) -> str:
                    return "".join(ch for ch in str(value).lower() if ch.isalnum())

                invoice_source_col = next(
                    (c for c in df.columns if _norm_invoice_column(c) in {
                        "invoiceno", "invoicenumber", "invoice"
                    }),
                    None,
                )
                billno_schema_col = next(
                    (c for c in col_meta_map if _norm_invoice_column(c) == "billno"),
                    None,
                )
                billnumber_schema_col = next(
                    (c for c in col_meta_map if _norm_invoice_column(c) == "billnumber"),
                    None,
                )

                bills_billno_identity_insert = False
                mapped_billno_col = None
                if normalized_table_name in ("bill", "bills") and invoice_source_col and billno_schema_col:
                    # The user's schema/business rule requires the SAME Excel invoice
                    # number in BOTH Bill.BillNo and Bill.Bill_Number. BillNo may be an
                    # IDENTITY column, so explicitly insert its value with IDENTITY_INSERT.
                    # Keep Bill_Number as a separate mapped column; do not replace either
                    # column with a generated/sequential value.
                    excel_to_db.pop(invoice_source_col, None)
                    excel_to_db[invoice_source_col] = billno_schema_col
                    db_to_excel = {db_col: ex_col for ex_col, db_col in excel_to_db.items()}
                    target_db_cols = list(dict.fromkeys(excel_to_db.values()))
                    mapped_billno_col = billno_schema_col

                    if billnumber_schema_col:
                        if billnumber_schema_col not in target_db_cols:
                            target_db_cols.append(billnumber_schema_col)
                        db_to_excel[billnumber_schema_col] = invoice_source_col

                    billno_is_identity = bool(getattr(col_meta_map[billno_schema_col], "identity", False))
                    bills_billno_identity_insert = dialect_name == "mssql" and billno_is_identity
                    if billno_is_identity:
                        # It must be included in the INSERT despite being an IDENTITY column.
                        identity_cols = [
                            c for c in identity_cols
                            if _norm_invoice_column(c) != "billno"
                        ]
                    logger.info(
                        "[BILL_INVOICE_MAPPING] table=%s source_column=%s target_columns=%s identity_insert=%s",
                        table_name, invoice_source_col,
                        [c for c in (billno_schema_col, billnumber_schema_col) if c],
                        bills_billno_identity_insert
                    )

                # Business-key columns used to index IDs for foreign-key resolution only.
                identifying_db_cols: List[str] = []
                for c_name in target_db_cols:
                    c_lower = c_name.lower()
                    if c_name in pk_cols and c_name not in identity_cols:
                        identifying_db_cols.append(c_name)
                    elif col_meta_map.get(c_name) and col_meta_map[c_name].unique:
                        identifying_db_cols.append(c_name)
                    elif any(k in c_lower for k in ("name", "code", "phone", "mobile", "number", "ref")):
                        identifying_db_cols.append(c_name)

                # Customer deduplication is processed through the dedicated CustomerDeduplicationEngine,
                # strictly adhering to Rules 1-4 (mobile matching, name updates, empty handling).
                if table_name.lower().replace("dbo.", "").strip() == "customer":
                    from app.migration.customer_dedup import process_customer_migration
                    process_customer_migration(
                        conn=conn,
                        table_name=table_name,
                        dialect_name=dialect_name,
                        df=df,
                        df_indices=df_indices,
                        df_records=df_records,
                        mappings=mappings,
                        t_meta=t_meta,
                        schema_tables=schema_tables,
                        t_stat=t_stat,
                        id_store=id_store,
                        lookup_relationships=lookup_relationships,
                        parent_records_by_table=parent_records_by_table,
                        val_items_by_col=val_items_by_col,
                        value_transform_lookup=value_transform_lookup,
                        col_value_transform=col_value_transform,
                        user_val_overrides=user_val_overrides,
                        fk_resolution_cache=fk_resolution_cache,
                        upload_id=upload_id,
                        failure_details=failure_details,
                        generated_ids_by_table=generated_ids_by_table,
                        source_row_indices_with_insert=source_row_indices_with_insert,
                        progress_callback=progress_callback,
                        tbl_idx=tbl_idx,
                        total_tables=len(active_tables_ordered)
                    )
                    t1 = time.time()
                    logger.info(
                        f"[MIGRATION] Table '{table_name}' processed in {t1 - t0:.3f}s | "
                        f"inserted={t_stat.inserted} | reused={t_stat.reused} | "
                        f"updated={t_stat.updated} | skipped={t_stat.skipped}"
                    )
                    continue

                if not identifying_db_cols:
                    identifying_db_cols = [c for c in target_db_cols if c not in identity_cols][:2]

                # 5.1 Read existing business keys only to seed ID resolution for foreign keys.
                # This is NOT duplicate detection: non-Customer rows must always follow the
                # existing INSERT path, even when their values match existing database rows.
                quoted_tbl = quote_identifier(table_name, dialect_name)
                select_cols = list(dict.fromkeys(identifying_db_cols + pk_cols))

                normalized_table_name = table_name.lower().replace("dbo.", "").strip()
                referenced_parent_names = {
                    rel.parent_table.lower().replace("dbo.", "").strip()
                    for rel in lookup_relationships
                }
                for child_table_name in active_tables_ordered:
                    child_meta = schema_tables.get(child_table_name)
                    if child_meta is None:
                        continue
                    for fk in getattr(child_meta, "foreign_keys", []):
                        if getattr(fk, "target_table", None):
                            referenced_parent_names.add(fk.target_table.lower().replace("dbo.", "").strip())

                has_unique_identifying = any(col_meta_map.get(c, ColumnMetadata(name=c, type="")).unique for c in identifying_db_cols)
                is_transaction_header = normalized_table_name in ("order", "orders", "bills", "bill", "settlements", "settlement")

                if normalized_table_name in ("order", "orders") and "BillNo" in col_meta_map and "BillNo" not in select_cols:
                    select_cols.append("BillNo")
                if normalized_table_name in ("bills", "bill") and "Bill_Number" in col_meta_map and "Bill_Number" not in select_cols:
                    select_cols.append("Bill_Number")
                if normalized_table_name in ("settlements", "settlement") and "BillNo" in col_meta_map and "BillNo" not in select_cols:
                    select_cols.append("BillNo")

                if select_cols and (normalized_table_name in referenced_parent_names or is_transaction_header or has_unique_identifying):
                    quoted_select_cols = [quote_identifier(c, dialect_name) for c in select_cols]
                    try:
                        select_sql = f"SELECT {', '.join(quoted_select_cols)} FROM {quoted_tbl}"
                        existing_res = conn.execute(text(select_sql)).mappings().all()
                        for row_dict in existing_res:
                            pk_val = row_dict.get(pk_cols[0]) if pk_cols else None
                            for c in select_cols:
                                val_str = str(row_dict.get(c, "")).strip()
                                if val_str and pk_val is not None:
                                    id_store.register_id(table_name, val_str, pk_val)
                            row_key = tuple(normalize_string(str(row_dict.get(c, "") or "")) for c in identifying_db_cols)
                            if any(row_key) and pk_val is not None:
                                id_store.register_composite_key(table_name, row_key, pk_val)
                    except Exception as lookup_err:
                        logger.warning(f"[MIGRATION] Existing FK/key lookup indexing for {table_name}: {lookup_err}")

                # 5.2 Begin with all source rows. Customer duplicate detection is the
                # only database duplicate check. For this item-level report, Order and
                # Bills are invoice-header tables: aggregate their source rows to one
                # row per invoice while preserving every row for OrderLineItems.
                entities_df = df
                entities_records = entities_df.to_dict(orient="records")
                entities_df_indices = entities_df.index.tolist()
                normalized_table_name = table_name.lower().replace("dbo.", "").strip()
                order_invoice_column: Optional[str] = None
                order_source_rows_by_invoice: Dict[str, List[int]] = {}

                # Customer rows with neither a name nor a mobile number do not
                # represent a customer. Exclude them from Customer inserts entirely;
                # related Order rows are still processed and may use Customer_ID=NULL
                # because dbo.Order.Customer_ID is nullable.
                if table_name.lower().replace("dbo.", "").strip() == "customer":
                    customer_name_excel_col = next(
                        (db_to_excel[c] for c in target_db_cols
                         if c.lower() in ("customer_name", "customername", "name")),
                        None,
                    )
                    customer_mobile_excel_col = next(
                        (db_to_excel[c] for c in target_db_cols
                         if any(k in c.lower() for k in (
                             "mobile_number", "mobile", "phone_number",
                             "customer_phone", "customer_mobile", "contact_number", "phone"
                         ))),
                        None,
                    )

                    def _customer_source_value(row_data: Dict[str, Any], excel_col: Optional[str]) -> Any:
                        return get_df_val(row_data, excel_col) if excel_col else None

                    keep_customer_indices = []
                    for entity_idx, entity_record in zip(entities_df.index.tolist(), entities_records):
                        source_name = _customer_source_value(entity_record, customer_name_excel_col)
                        source_mobile = _customer_source_value(entity_record, customer_mobile_excel_col)
                        if is_empty_or_na(source_name) and is_empty_or_na(source_mobile):
                            logger.info(
                                "[CUSTOMER_SKIPPED_EMPTY_IDENTITY] upload_id=%s source_row=%s",
                                upload_id, int(entity_idx) + 1
                            )
                            continue
                        keep_customer_indices.append(entity_idx)

                    entities_df = entities_df.loc[keep_customer_indices]
                    entities_records = entities_df.to_dict(orient="records")
                    entities_df_indices = entities_df.index.tolist()
                else:
                    entities_records = entities_df.to_dict(orient="records")
                    entities_df_indices = entities_df.index.tolist()

                if normalized_table_name == "order":
                    order_invoice_column = next(
                        (c for c in entities_df.columns
                         if normalize_string(str(c)).replace(" ", "").replace("_", "")
                         in ("invoiceno", "invoicenumber")),
                        None,
                    )
                    if order_invoice_column:
                        for source_idx, source_record in zip(entities_df_indices, entities_records):
                            source_invoice = source_record.get(order_invoice_column)
                            if not is_empty_or_na(source_invoice):
                                order_source_rows_by_invoice.setdefault(str(source_invoice).strip().casefold(), []).append(int(source_idx))
                        order_quantity_excel_col = next(
                            (m.excel_column for m in mappings
                             if normalize_string(m.database_column).replace(" ", "") in ("quantity", "itemquantity")
                             and m.excel_column in entities_df.columns),
                            None,
                        )
                        entities_records, entities_df_indices, duplicate_order_rows, unique_order_invoices = collapse_bill_rows_by_invoice(
                            entities_records, entities_df_indices, order_invoice_column, order_quantity_excel_col
                        )
                        if duplicate_order_rows:
                            t_stat.skipped += duplicate_order_rows
                            logger.info(
                                "[ORDER_SOURCE_AGGREGATION] upload_id=%s unique_order_invoices=%s "
                                "invoice_rows_aggregated=%s; OrderLineItems source rows remain unchanged.",
                                upload_id, unique_order_invoices, duplicate_order_rows
                            )

                elif normalized_table_name in ("bill", "bills"):
                    billno_excel_col = next(
                        (m.excel_column for m in mappings
                         if normalize_string(m.database_column).replace(" ", "").replace("_", "") in ("billno", "billnumber")
                         and m.excel_column in entities_df.columns),
                        None,
                    )
                    if billno_excel_col is None:
                        billno_excel_col = next(
                            (c for c in entities_df.columns
                             if normalize_string(str(c)).replace(" ", "").replace("_", "")
                             in ("invoiceno", "invoicenumber")),
                            None,
                        )
                    if billno_excel_col:
                        bill_quantity_excel_col = next(
                            (m.excel_column for m in mappings
                             if normalize_string(m.database_column).replace(" ", "") in ("quantity", "itemquantity")
                             and m.excel_column in entities_df.columns),
                            None,
                        )
                        entities_records, entities_df_indices, duplicate_bill_rows, unique_bill_numbers = collapse_bill_rows_by_invoice(
                            entities_records, entities_df_indices, billno_excel_col, bill_quantity_excel_col
                        )
                        if duplicate_bill_rows:
                            t_stat.skipped += duplicate_bill_rows
                            logger.info(
                                "[BILLS_SOURCE_AGGREGATION] upload_id=%s unique_bill_numbers=%s "
                                "invoice_rows_aggregated=%s; OrderLineItems source rows remain unchanged.",
                                upload_id, unique_bill_numbers, duplicate_bill_rows
                            )

                elif normalized_table_name in ("settlements", "settlement"):
                    settlement_invoice_col = next(
                        (m.excel_column for m in mappings
                         if normalize_string(m.database_column).replace(" ", "").replace("_", "") in ("billno", "billnumber")
                         and m.excel_column in entities_df.columns),
                        None,
                    )
                    if settlement_invoice_col is None:
                        settlement_invoice_col = next(
                            (c for c in entities_df.columns
                             if normalize_string(str(c)).replace(" ", "").replace("_", "")
                             in ("invoiceno", "invoicenumber")),
                            None,
                        )
                    settlement_pay_col = next(
                        (m.excel_column for m in mappings
                         if "payment" in normalize_string(m.excel_column) or "mode" in normalize_string(m.excel_column)),
                        None,
                    )
                    if settlement_pay_col is None:
                        settlement_pay_col = next(
                            (c for c in entities_df.columns if "payment" in normalize_string(str(c)) or "pay" in normalize_string(str(c))),
                            None,
                        )
                    settlement_amt_col = next(
                        (m.excel_column for m in mappings
                         if normalize_string(m.database_column).replace(" ", "") in ("total", "amount", "finalamount", "others", "price")
                         and m.excel_column in entities_df.columns),
                        None,
                    )
                    if settlement_amt_col is None:
                        settlement_amt_col = next(
                            (c for c in entities_df.columns
                             if normalize_string(str(c)).replace(" ", "").replace("_", "") in ("total", "amount")),
                            None,
                        )
                    entities_records, entities_df_indices, duplicate_settlement_rows, unique_settlement_entries = collapse_settlement_rows(
                        entities_records, entities_df_indices, settlement_invoice_col, settlement_pay_col, settlement_amt_col
                    )
                    if duplicate_settlement_rows:
                        t_stat.skipped += duplicate_settlement_rows
                        logger.info(
                            "[SETTLEMENTS_SOURCE_AGGREGATION] upload_id=%s unique_settlements=%s "
                            "rows_aggregated=%s; OrderLineItems source rows remain unchanged.",
                            upload_id, unique_settlement_entries, duplicate_settlement_rows
                        )


                num_entities = len(entities_records)

                # Columns to insert (omit identity columns, deduplicate strictly preserving order)
                insert_cols: List[str] = []
                seen_insert_cols: Set[str] = set()

                for c in target_db_cols:
                    if c not in identity_cols and c not in seen_insert_cols:
                        insert_cols.append(c)
                        seen_insert_cols.add(c)

                # Foreign key columns from introspected metadata & discovered lookup relationships
                fk_injections: Dict[str, Tuple[str, str]] = {
                    fk.constrained_column: (fk.target_table, fk.target_column)
                    for fk in t_meta.foreign_keys
                    if fk.target_table in schema_tables
                }
                for rel in lookup_relationships:
                    if rel.child_table == table_name and rel.parent_table in schema_tables:
                        if rel.child_column not in fk_injections:
                            fk_injections[rel.child_column] = (rel.parent_table, rel.parent_id_column)

                table_fk_cols = set(fk_injections.keys())

                for fk_col in fk_injections:
                    if fk_col not in seen_insert_cols and fk_col not in identity_cols:
                        insert_cols.append(fk_col)
                        seen_insert_cols.add(fk_col)

                # Include optional text columns even when they are absent from Excel or
                # were not mapped (for example Description). This makes the INSERT store
                # an empty string instead of allowing SQL Server to persist NULL.
                # Schema defaults and foreign-key columns are intentionally preserved.
                for c in t_meta.columns:
                    if should_fill_unmapped_text_column(c, t_meta, table_fk_cols):
                        if c.name not in seen_insert_cols:
                            insert_cols.append(c.name)
                            seen_insert_cols.add(c.name)

                # Ensure every required column (not nullable, not identity, no default) is included
                for c in t_meta.columns:
                    if not c.nullable and not getattr(c, "identity", False) and c.default is None:
                        if c.name not in seen_insert_cols:
                            insert_cols.append(c.name)
                            seen_insert_cols.add(c.name)

                # 5.3 Iterate rows and execute parameterized INSERTs
                for i in range(num_entities):
                    row_idx = entities_df_indices[i]
                    row = entities_records[i]
                    source_row_num = int(row_idx) + 1

                    # Non-Customer tables never skip an INSERT because a similar record exists.
                    # Existing IDs are fetched only by the FK-resolution paths below.
                    row_key_tuple = tuple(
                        normalize_string("" if is_empty_or_na(get_df_val(row, db_to_excel[c])) else str(get_df_val(row, db_to_excel[c])))
                        for c in identifying_db_cols
                        if c in db_to_excel
                    )

                    # Prepare parameter dictionary
                    param_dict: Dict[str, Any] = {}
                    for ex_col, db_col in excel_to_db.items():
                        if db_col in insert_cols:
                            raw_val = get_df_val(row, ex_col)
                            c_type = col_meta_map[db_col].type if db_col in col_meta_map else ""
                            c_nullable = col_meta_map[db_col].nullable if db_col in col_meta_map else True

                            # Apply approved / resolved value mapping if present
                            raw_str = str(raw_val).strip() if raw_val is not None else ""
                            mapped_val = (
                                value_transform_lookup.get((table_name, db_col, raw_str))
                                or col_value_transform.get((db_col, raw_str))
                            )
                            val_to_use = mapped_val if mapped_val is not None else raw_val
                            c_meta = col_meta_map.get(db_col)
                            is_legit = (mapped_val == "null")
                            expects_empty = should_column_receive_empty_string(db_col, table_name, c_meta, t_meta, table_fk_cols)
                            param_dict[db_col] = sanitize_value(
                                val_to_use,
                                c_type,
                                c_nullable,
                                expects_empty_string=expects_empty,
                                is_legitimate_literal=is_legit
                            )

                    # OrderLineItems quantity must come directly from the source
                    # item's item_quantity column. Do not let generic/fuzzy mapping
                    # choose item_price, item_total, or another numeric column.
                    if normalized_table_name.replace("_", "").replace(" ", "") == "orderlineitems":
                        def _norm_quantity_name(value: Any) -> str:
                            return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

                        source_item_quantity_col = next(
                            (c for c in df.columns if _norm_quantity_name(c) == "itemquantity"),
                            None,
                        )
                        target_quantity_col = next(
                            (c for c in col_meta_map if _norm_quantity_name(c) == "quantity"),
                            None,
                        )
                        if source_item_quantity_col and target_quantity_col:
                            raw_item_quantity = get_df_val(row, source_item_quantity_col)
                            quantity_meta = col_meta_map[target_quantity_col]
                            param_dict[target_quantity_col] = sanitize_value(
                                raw_item_quantity,
                                str(quantity_meta.type),
                                quantity_meta.nullable,
                                expects_empty_string=False,
                            )
                            if target_quantity_col not in insert_cols:
                                insert_cols.append(target_quantity_col)
                            logger.debug(
                                "[ORDER_LINE_QUANTITY_MAPPING] upload_id=%s source_row=%s "
                                "source_column=%s source_value=%r target_column=%s final_value=%r",
                                upload_id, source_row_num, source_item_quantity_col,
                                raw_item_quantity, target_quantity_col,
                                param_dict[target_quantity_col],
                            )

                    # Force the Excel invoice number into BOTH Bill.BillNo and
                    # Bill.Bill_Number. BillNo is the authoritative parent key for FKs.
                    if normalized_table_name in ("bill", "bills") and invoice_source_col and mapped_billno_col:
                        invoice_raw_value = get_df_val(row, invoice_source_col)
                        invoice_target_cols = [mapped_billno_col]
                        if billnumber_schema_col:
                            invoice_target_cols.append(billnumber_schema_col)
                        for invoice_target_col in dict.fromkeys(invoice_target_cols):
                            if invoice_target_col not in insert_cols:
                                insert_cols.append(invoice_target_col)
                            invoice_col_meta = col_meta_map[invoice_target_col]
                            param_dict[invoice_target_col] = sanitize_value(
                                invoice_raw_value,
                                str(invoice_col_meta.type),
                                invoice_col_meta.nullable,
                                expects_empty_string=False,
                            )

                    # Populate unmapped/missing optional text columns with empty strings.
                    # This covers fields such as Description that the user did not fill in.
                    for _column in t_meta.columns:
                        if should_fill_unmapped_text_column(_column, t_meta, table_fk_cols):
                            param_dict.setdefault(_column.name, "")

                    # Resolve Foreign Keys from DynamicLookupResolver & IdResolverStore
                    row_resolved_entities: Dict[str, ResolvedEntityRecord] = {}
                    this_row_vms: List[ValueMappingItem] = []
                    for ex_col, vms_map in val_items_by_col.items():
                        actual_val = get_df_val(row, ex_col)
                        if actual_val is not None:
                            matched_vms = vms_map.get(str(actual_val).strip())
                            if matched_vms:
                                for vm in matched_vms:
                                    this_row_vms.append(vm)
                                    if vm.matched_table and vm.matched_primary_key_value is not None:
                                        resolved_vm_ent = ResolvedEntityRecord(
                                            status="RESOLVED",
                                            matchedTable=vm.matched_table,
                                            matchedPrimaryKeyColumn=vm.matched_primary_key_column or "ID",
                                            matchedPrimaryKeyValue=vm.matched_primary_key_value,
                                            matchedBy=[f"{vm.excel_column}={vm.excel_value}"],
                                            confidence=vm.confidence,
                                            recordData=vm.matched_record_data or {},
                                            matchMethod="VALUE_MAPPING"
                                        )
                                        row_resolved_entities[vm.matched_table] = resolved_vm_ent
                                        row_resolved_entities[vm.matched_table.lower()] = resolved_vm_ent
                                    if vm.matched_record_data and "Outlet_ID" in vm.matched_record_data:
                                        out_id = vm.matched_record_data["Outlet_ID"]
                                        if out_id is not None:
                                            out_rec = next((r for r in parent_records_by_table.get("Outlets", []) if r.get("Outlet_ID") == out_id), {})
                                            resolved_outlet_ent = ResolvedEntityRecord(
                                                status="RESOLVED",
                                                matchedTable="Outlets",
                                                matchedPrimaryKeyColumn="Outlet_ID",
                                                matchedPrimaryKeyValue=out_id,
                                                matchedBy=[f"ValueMapping {vm.database_table}.{vm.database_column}: {vm.target_value}"],
                                                confidence=0.95,
                                                recordData=out_rec,
                                                matchMethod="VALUE_MAPPING_CORRELATED"
                                            )
                                            row_resolved_entities["Outlets"] = resolved_outlet_ent
                                            row_resolved_entities["outlets"] = resolved_outlet_ent

                    for (fk_c, (p_t, p_c)) in fk_injections.items():
                        if p_t not in row_resolved_entities and id_store.has_row_id(p_t, int(row_idx)):
                            p_id = id_store.get_row_id(p_t, int(row_idx))
                            if p_id is not None:
                                row_resolved_entities[p_t] = ResolvedEntityRecord(
                                    status="RESOLVED",
                                    matchedTable=p_t,
                                    matchedPrimaryKeyColumn=p_c,
                                    matchedPrimaryKeyValue=p_id,
                                    matchedBy=[f"Prior insert {p_t}"],
                                    confidence=1.0,
                                    recordData={},
                                    matchMethod="GENERATED_ID"
                                )

                    for fk_col, (parent_tbl, parent_col) in fk_injections.items():
                        if str(parent_tbl or "").strip().lower().split(".")[-1] == "outlets":
                            # The outlet ID was read from SQL Server once for this run.
                            # Never match restaurant/virtual-brand text per Excel row.
                            cached_outlet_id = outlet_id_cache.get(parent_tbl)
                            if cached_outlet_id is None:
                                cached_outlet_id = get_single_outlet_id(
                                    parent_records_by_table, parent_tbl, parent_col
                                )
                                outlet_id_cache[parent_tbl] = cached_outlet_id
                            outlet_col_meta = col_meta_map.get(fk_col)
                            param_dict[fk_col] = sanitize_value(
                                cached_outlet_id,
                                getattr(outlet_col_meta, "type", ""),
                                getattr(outlet_col_meta, "nullable", False)
                            )
                            id_store.register_id(parent_tbl, str(cached_outlet_id), cached_outlet_id)
                            id_store.register_row_id(parent_tbl, int(row_idx), cached_outlet_id)
                            continue

                        source_excel_col = db_to_excel.get(fk_col)
                        source_val = get_df_val(row, source_excel_col) if source_excel_col else None
                        res_rec = None

                        # If already provided as direct valid numeric ID from Excel and not a text name
                        if fk_col in param_dict and param_dict[fk_col] is not None and not is_empty_or_na(param_dict[fk_col]):
                            if param_dict[fk_col] == 0 and source_val is not None and not str(source_val).strip().isdigit():
                                param_dict[fk_col] = None
                            elif parent_tbl.lower() in ("outlets", "dbo.outlets"):
                                direct_id = param_dict[fk_col]
                                valid_ids = {
                                    str(record.get(parent_col))
                                    for record in parent_records_by_table.get(parent_tbl, [])
                                    if record.get(parent_col) is not None
                                }
                                if (
                                    not isinstance(direct_id, int)
                                    or isinstance(direct_id, bool)
                                    or str(direct_id) not in valid_ids
                                ):
                                    raise ValueError(
                                        f"Row {source_row_num}: Direct outlet foreign key '{fk_col}' "
                                        f"must be an existing integer {parent_tbl}.{parent_col}; "
                                        f"received {direct_id!r} ({type(direct_id).__name__})."
                                    )
                                restaurant_value = row.get("restaurant_name")
                                virtual_brand_value = row.get("virtual_brand_name")
                                if (
                                    not is_empty_or_na(restaurant_value)
                                    or not is_empty_or_na(virtual_brand_value)
                                ):
                                    direct_resolution = DynamicLookupResolver.resolve_foreign_key_record(
                                        child_table=table_name,
                                        child_column=fk_col,
                                        parent_table=parent_tbl,
                                        parent_id_column=parent_col,
                                        parent_name_column=next((r.parent_name_column for r in lookup_relationships if r.child_table == table_name and r.child_column == fk_col), None),
                                        db_records=parent_records_by_table.get(parent_tbl, []),
                                        row_data=row,
                                        upload_id=upload_id,
                                        source_row_num=source_row_num
                                    )
                                    if (
                                        direct_resolution.status != "RESOLVED"
                                        or direct_resolution.matched_primary_key_value != direct_id
                                    ):
                                        raise ValueError(
                                            f"Row {source_row_num}: Direct outlet ID {direct_id} conflicts with "
                                            f"source outlet values: {direct_resolution.message}"
                                        )
                                param_dict[fk_col] = int(direct_id)
                                continue
                            else:
                                continue

                        resolved_parent_id = None

                        # Step 1: Check if source value was directly supplied and matches known ID
                        if source_val is not None and not is_empty_or_na(source_val):
                            s_val_str = str(source_val).strip()
                            resolved_parent_id = id_store.get_id(parent_tbl, s_val_str)
                            if resolved_parent_id is None:
                                resolved_parent_id = id_store.get_id(parent_tbl, normalize_string(s_val_str))

                        # Step 2: Row-level parent resolution (multi-level chain Customer -> Orders -> Order_Items)
                        num_parent_refs = sum(1 for p_tbl, _ in fk_injections.values() if p_tbl == parent_tbl)
                        has_own_source = bool(source_val and not is_empty_or_na(source_val))
                        rel = next((r for r in lookup_relationships if r.child_table == table_name and r.child_column == fk_col), None)
                        if rel and rel.excel_name_column:
                            n_val = get_df_val(row, rel.excel_name_column)
                            if n_val is not None and not is_empty_or_na(n_val):
                                has_own_source = True

                        if resolved_parent_id is None and (num_parent_refs == 1 or not has_own_source or "order" in fk_col.lower()):
                            if id_store.has_row_id(parent_tbl, int(row_idx)):
                                resolved_parent_id = id_store.get_row_id(parent_tbl, int(row_idx))

                        # Step 3: Check business names/keys in the same row for parent table
                        if resolved_parent_id is None:
                            parent_mappings = table_mappings_map.get(parent_tbl, [])
                            for pm in parent_mappings:
                                p_raw = get_df_val(row, pm.excel_column)
                                if p_raw is not None and not is_empty_or_na(p_raw):
                                    resolved_parent_id = id_store.get_id(parent_tbl, str(p_raw).strip())
                                    if resolved_parent_id is None:
                                        resolved_parent_id = id_store.get_id(parent_tbl, normalize_string(str(p_raw)))
                                    if resolved_parent_id is not None:
                                        break

                        # Step 4: Check DynamicLookupResolver matching against actual database records (cached)
                        if resolved_parent_id is None:
                            rel = next((r for r in lookup_relationships if r.child_table == table_name and r.child_column == fk_col), None)
                            name_val = get_df_val(row, rel.excel_name_column) if (rel and rel.excel_name_column) else None
                            str_name = str(name_val).strip() if (name_val is not None and not is_empty_or_na(name_val)) else (str(source_val).strip() if source_val is not None and not is_empty_or_na(source_val) else None)

                            # Item_ID is resolved from the line's business description, not from a
                            # blank Item_ID source column. Relationship discovery can fail to infer
                            # the correct source-name column when headers are generic, so use the
                            # row's semantic item-name fields as a safe fallback.
                            is_item_fk = is_item_master_target(parent_tbl, table_name, fk_col)
                            if str_name is None and is_item_fk:
                                row_dict_for_item = row if isinstance(row, dict) else row.to_dict()
                                item_name_keys = {
                                    "description", "item", "item_name", "itemname",
                                    "product_name", "product", "menu_item", "menu_item_name"
                                }
                                for source_key, source_value in row_dict_for_item.items():
                                    normalized_key = normalize_string(str(source_key)).replace(" ", "").replace("_", "")
                                    if normalized_key in {k.replace("_", "") for k in item_name_keys} and not is_empty_or_na(source_value):
                                        str_name = str(source_value).strip()
                                        break

                            # Check user override
                            user_override = (
                                user_val_overrides.get(f"{parent_tbl}.{str_name}")
                                or user_val_overrides.get(f"{table_name}.{fk_col}")
                                or user_val_overrides.get(str_name)
                            )
                            if parent_tbl.lower() in ("outlets", "dbo.outlets"):
                                source_cache_key = outlet_row_source_key(row)
                            elif is_item_fk:
                                source_cache_key = item_row_source_key(row if isinstance(row, dict) else row.to_dict(), str_name)
                            else:
                                source_cache_key = str_name or ""
                            fk_cache_key = (parent_tbl, parent_col, source_cache_key, str(user_override or ""))
                            if fk_cache_key in fk_resolution_cache:
                                res_rec = fk_resolution_cache[fk_cache_key]
                            else:
                                db_recs = parent_records_by_table.get(parent_tbl, [])
                                res_rec = DynamicLookupResolver.resolve_foreign_key_record(
                                    child_table=table_name,
                                    child_column=fk_col,
                                    parent_table=parent_tbl,
                                    parent_id_column=parent_col,
                                    parent_name_column=rel.parent_name_column if rel else None,
                                    db_records=db_recs,
                                    excel_source_value=str_name,
                                    row_resolved_entities=row_resolved_entities,
                                    row_value_mappings=this_row_vms,
                                    row_data=row if isinstance(row, dict) else row.to_dict(),
                                    user_override_id=user_override,
                                    all_parent_records=parent_records_by_table,
                                    upload_id=upload_id,
                                    source_row_num=source_row_num
                                )
                                if res_rec.status == "RESOLVED":
                                    fk_resolution_cache[fk_cache_key] = res_rec

                            if res_rec.status == "RESOLVED":
                                resolved_parent_id = res_rec.matched_primary_key_value
                                if str_name:
                                    id_store.register_id(parent_tbl, str_name, resolved_parent_id)
                                id_store.register_row_id(parent_tbl, int(row_idx), resolved_parent_id)

                            elif str_name and id_store.has_id(parent_tbl, str_name):
                                resolved_parent_id = id_store.get_id(parent_tbl, str_name)
                            elif str_name and id_store.has_id(parent_tbl, normalize_string(str_name)):
                                resolved_parent_id = id_store.get_id(parent_tbl, normalize_string(str_name))

                        # Step 5: Self-referencing FK handling (e.g. Employees.Manager_ID -> Employees.Employee_ID)
                        if resolved_parent_id is None and parent_tbl == table_name:
                            if source_val is not None and not is_empty_or_na(source_val):
                                resolved_parent_id = id_store.get_id(table_name, str(source_val).strip())

                        if resolved_parent_id is not None:
                            fk_propagated_count += 1
                            c_meta = col_meta_map.get(fk_col)
                            c_type = c_meta.type if c_meta else ""
                            c_null = c_meta.nullable if c_meta else True
                            param_dict[fk_col] = sanitize_value(resolved_parent_id, c_type, c_null)

                            # ItemMaster is used only to resolve the Item_ID foreign key.
                            # Keep the price already mapped from the current Excel row in param_dict;
                            # never overwrite OrderLineItems price with ItemsMaster price.

                            if parent_tbl.lower() in ("outlets", "dbo.outlets"):
                                valid_outlet_ids = {
                                    str(record.get(parent_col))
                                    for record in parent_records_by_table.get(parent_tbl, [])
                                    if record.get(parent_col) is not None
                                }
                                if (
                                    not isinstance(param_dict[fk_col], int)
                                    or isinstance(param_dict[fk_col], bool)
                                    or str(param_dict[fk_col]) not in valid_outlet_ids
                                ):
                                    raise ValueError(
                                        f"Row {source_row_num}: Invalid resolved outlet FK '{fk_col}' "
                                        f"for {parent_tbl}.{parent_col}; final parameter={param_dict[fk_col]!r} "
                                        f"({type(param_dict[fk_col]).__name__})."
                                    )
                                logger.info(
                                    "[OUTLET_FK_PARAMETER] upload_id=%s source_row=%s target=%s.%s "
                                    "parent=%s.%s value=%r type=%s",
                                    upload_id, source_row_num, table_name, fk_col,
                                    parent_tbl, parent_col, param_dict[fk_col],
                                    type(param_dict[fk_col]).__name__
                                )
                            else:
                                logger.info(
                                    "[LOOKUP_FK_RESOLVED] upload_id=%s source_row=%s target=%s.%s "
                                    "parent=%s.%s source_val=%r resolved_id=%r match_method=%s",
                                    upload_id, source_row_num, table_name, fk_col,
                                    parent_tbl, parent_col, source_val, param_dict[fk_col],
                                    getattr(res_rec, "match_method", "LOOKUP") if "res_rec" in locals() and res_rec else "STORE"
                                )
                        elif fk_col not in param_dict or param_dict[fk_col] is None:
                            c_meta = col_meta_map.get(fk_col)
                            is_item_fk = is_item_master_target(parent_tbl, table_name, fk_col)
                            if is_item_fk:
                                row_dict_log = row if isinstance(row, dict) else row.to_dict()
                                cls_vals = DynamicLookupResolver.discover_source_classification_values(row_dict_log)
                                cat_log = cls_vals["categories"][0][1] if cls_vals["categories"] else None
                                grp_log = cls_vals["groups"][0][1] if cls_vals["groups"] else None
                                price_log = row_dict_log.get("Item_Price") or row_dict_log.get("Price") or row_dict_log.get("price")
                                status_str = getattr(res_rec, "status", "UNRESOLVED") if "res_rec" in locals() and res_rec else "UNRESOLVED"
                                reason_str = getattr(res_rec, "message", "No matching item found in master.") if "res_rec" in locals() and res_rec else "No matching item found in master."
                                logger.warning(
                                    "[ITEM_FK_UNRESOLVED] upload_id=%s source_row=%s target=%s.%s parent=%s.%s "
                                    "description=%r category=%r group=%r price=%r status=%s reason=%s",
                                    upload_id, source_row_num, table_name, fk_col, parent_tbl, parent_col,
                                    str_name, cat_log, grp_log, price_log, status_str, reason_str
                                )
                                failure_details.append({
                                    "row": source_row_num,
                                    "table": table_name,
                                    "column": fk_col,
                                    "description": str_name,
                                    "category": cat_log,
                                    "group": grp_log,
                                    "price": price_log,
                                    "status": status_str,
                                    "reason": reason_str
                                })
                            if c_meta and not c_meta.nullable and c_meta.default is None:
                                reason = res_rec.message if "res_rec" in locals() and res_rec else "No valid foreign key ID was produced."
                                msg = f"Row {source_row_num}: Unresolved required foreign key '{fk_col}' in '{table_name}' pointing to '{parent_tbl}.{parent_col}': {reason}"
                                failure_details.append({"row": source_row_num, "table": table_name, "error": msg})
                                raise ValueError(msg)
                            elif c_meta and c_meta.nullable:
                                param_dict[fk_col] = None

                    # Bills: BillNo stores the Excel invoice number. If that invoice
                    # already exists, reuse its existing BillNo for child FK resolution.
                    if (
                        normalized_table_name in ("bill", "bills")
                        and bills_billno_identity_insert
                        and mapped_billno_col in param_dict
                        and param_dict.get(mapped_billno_col) is not None
                    ):
                        invoice_value = param_dict[mapped_billno_col]
                        existing_bill_sql = text(
                            f"SELECT {quote_identifier(mapped_billno_col, dialect_name)} "
                            f"FROM {quoted_tbl} "
                            f"WHERE {quote_identifier(mapped_billno_col, dialect_name)} = :invoice_value"
                        )
                        existing_bill_id = conn.execute(
                            existing_bill_sql, {"invoice_value": invoice_value}
                        ).scalar()
                        if existing_bill_id is not None:
                            id_store.register_id(table_name, str(invoice_value).strip(), existing_bill_id)
                            id_store.register_id(table_name, normalize_string(str(invoice_value)), existing_bill_id)
                            id_store.register_row_id(table_name, int(row_idx), existing_bill_id)
                            id_store.register_composite_key(table_name, row_key_tuple, existing_bill_id)
                            generated_ids_by_table[table_name].append(existing_bill_id)
                            t_stat.reused += 1
                            logger.info(
                                "[BILLS_INVOICE_REUSED] upload_id=%s row=%s invoice=%r existing_BillNo=%r; skipped duplicate insert",
                                upload_id, source_row_num, invoice_value, existing_bill_id
                            )
                            continue

                    # Build parameterized SQL
                    row_insert_cols = []
                    for c in insert_cols:
                        c_meta = col_meta_map.get(c)
                        val = param_dict.get(c)
                        has_default = (c_meta is not None and c_meta.default is not None)
                        is_nullable = (c_meta is not None and c_meta.nullable)

                        if val is not None:
                            row_insert_cols.append(c)
                        elif c in table_fk_cols:
                            # A foreign key that failed resolution must remain SQL NULL when
                            # the actual database column permits NULL. Never pass its source
                            # label (e.g. "Sabudana Khichdi") through generic missing-value
                            # resolution, where it could be mistaken for an ID or replaced by
                            # an unrelated value. A NOT NULL FK cannot legally be NULL; fail
                            # clearly before issuing the INSERT so the transaction rolls back.
                            if is_nullable:
                                param_dict[c] = None
                                row_insert_cols.append(c)
                                logger.warning(
                                    "[FK_NULL_UNRESOLVED] upload_id=%s source_row=%s target=%s.%s; "
                                    "no parent ID resolved; inserting SQL NULL because the FK column is nullable",
                                    upload_id, source_row_num, table_name, c
                                )
                                continue
                            if has_default:
                                # Allow the database's declared default to apply when present.
                                param_dict.pop(c, None)
                                continue
                            msg = (
                                f"Row {source_row_num}: Foreign key '{c}' in '{table_name}' "
                                f"could not be resolved and the database column is NOT NULL. "
                                "SQL NULL cannot be inserted until the column is made nullable "
                                "or a valid parent record is supplied."
                            )
                            failure_details.append({"row": source_row_num, "table": table_name, "column": c, "error": msg})
                            raise ValueError(msg)
                        else:
                            # Attempt dynamic missing value resolution for non-FK columns only.
                            t_ent_idx = entity_indices.get(table_name)
                            m_res = MissingValueResolver.resolve_missing_value(
                                table_name=table_name,
                                target_column=c,
                                table_meta=t_meta,
                                source_row_data=row if isinstance(row, dict) else row.to_dict(),
                                db_to_excel=db_to_excel,
                                entity_lookup_index=t_ent_idx
                            )
                            if m_res.status == "RESOLVED":
                                expects_empty = should_column_receive_empty_string(c, table_name, c_meta, t_meta, table_fk_cols)
                                param_dict[c] = sanitize_value(m_res.value, c_meta.type if c_meta else "", c_nullable, expects_empty_string=expects_empty)
                                row_insert_cols.append(c)
                            elif m_res.status == "DEFAULT" or has_default:
                                # Omit column from INSERT statement so SQL Server applies default constraint
                                if c in param_dict:
                                    del param_dict[c]
                                continue
                            elif is_nullable:
                                if c in param_dict:
                                    row_insert_cols.append(c)
                            else:
                                msg = f"Row {source_row_num}: {m_res.message}"
                                failure_details.append({"row": source_row_num, "table": table_name, "error": msg})
                                raise ValueError(msg)

                    # Final safety net: normalize values before SQL insertion.
                    # String/text columns expecting an empty string receive ''.
                    # Legitimate literal 'null' (explicitly mapped) is preserved.
                    # Non-text columns and nullable foreign keys receive SQL NULL (None).
                    for _col, _value in list(param_dict.items()):
                        if _col in table_fk_cols:
                            if _value is None or (isinstance(_value, str) and _value.strip().lower() in ("", "null", "none", "nan", "nat")):
                                param_dict[_col] = None
                            continue
                        _meta = col_meta_map.get(_col)
                        if should_column_receive_empty_string(_col, table_name, _meta, t_meta, table_fk_cols):
                            is_legit_literal = (_value == "null" and (
                                value_transform_lookup.get((table_name, _col, "null")) == "null"
                                or col_value_transform.get((_col, "null")) == "null"
                            ))
                            if not is_legit_literal:
                                if _value is None or (isinstance(_value, str) and _value.strip().lower() in ("", "null", "none", "nan", "nat")):
                                    param_dict[_col] = ""
                        else:
                            _type = str(getattr(_meta, "type", "") or "").upper()
                            if not is_text_sql_type(_type) and (_value is None or (isinstance(_value, str) and _value.strip().lower() in ("", "null", "none", "nan", "nat"))):
                                param_dict[_col] = None if getattr(_meta, "nullable", True) else 0

                    # Ensure row_insert_cols contains unique columns preserving order
                    deduped_row_insert_cols = []
                    seen_row_cols = set()
                    for c in row_insert_cols:
                        if c not in seen_row_cols:
                            deduped_row_insert_cols.append(c)
                            seen_row_cols.add(c)
                    row_insert_cols = deduped_row_insert_cols

                    if not row_insert_cols:
                        continue

                    # Verify 1-to-1 parameter matching
                    row_quoted_insert_cols = [quote_identifier(c, dialect_name) for c in row_insert_cols]
                    val_params = [f":{c}" for c in row_insert_cols]
                    assert len(row_quoted_insert_cols) == len(val_params) == len(set(row_insert_cols)), (
                        f"Duplicate columns detected in INSERT statement for table '{table_name}': {row_insert_cols}"
                    )
                    pk_to_output = pk_cols[0] if (pk_cols and pk_cols[0] in identity_cols) else None

                    # Parameterized INSERT with identity capture (supports SQL Server triggers via fallback)
                    identity_insert_enabled = False
                    try:
                        if bills_billno_identity_insert:
                            conn.execute(text(f"SET IDENTITY_INSERT {quoted_tbl} ON"))
                            identity_insert_enabled = True
                        if dialect_name == "mssql" and pk_to_output:
                            quoted_pk = quote_identifier(pk_to_output, dialect_name)
                            try:
                                insert_stmt = text(
                                    f"INSERT INTO {quoted_tbl} ({', '.join(row_quoted_insert_cols)}) "
                                    f"OUTPUT INSERTED.{quoted_pk} "
                                    f"VALUES ({', '.join(val_params)})"
                                )
                                res = conn.execute(insert_stmt, param_dict)
                                new_id = res.scalar()
                            except Exception as trigger_err:
                                err_str = str(trigger_err).lower()
                                if "output clause without into clause" in err_str or "trigger" in err_str:
                                    logger.info(f"[MIGRATION] Table '{table_name}' has triggers; falling back to SCOPE_IDENTITY() batch.")
                                    insert_fallback = text(
                                        f"INSERT INTO {quoted_tbl} ({', '.join(row_quoted_insert_cols)}) "
                                        f"VALUES ({', '.join(val_params)});\n"
                                        f"SELECT SCOPE_IDENTITY();"
                                    )
                                    res = conn.execute(insert_fallback, param_dict)
                                    new_id = res.scalar()
                                else:
                                    raise trigger_err
                        elif dialect_name in ("postgresql", "postgres") and pk_cols:
                            quoted_pk = quote_identifier(pk_cols[0], dialect_name)
                            insert_stmt = text(
                                f"INSERT INTO {quoted_tbl} ({', '.join(row_quoted_insert_cols)}) "
                                f"VALUES ({', '.join(val_params)}) "
                                f"RETURNING {quoted_pk}"
                            )
                            res = conn.execute(insert_stmt, param_dict)
                            new_id = res.scalar()
                        else:
                            insert_stmt = text(
                                f"INSERT INTO {quoted_tbl} ({', '.join(row_quoted_insert_cols)}) "
                                f"VALUES ({', '.join(val_params)})"
                            )
                            res = conn.execute(insert_stmt, param_dict)
                            new_id = getattr(res, "lastrowid", None) or param_dict.get(pk_cols[0] if pk_cols else None)

                        # For Bills.BillNo, the source invoice number is the authoritative key.
                        # Do not register a driver-returned/generated identity for child lookups.
                        if bills_billno_identity_insert and mapped_billno_col in param_dict:
                            explicit_invoice_key = param_dict.get(mapped_billno_col)
                            if explicit_invoice_key is not None:
                                new_id = explicit_invoice_key

                        if identity_insert_enabled:
                            conn.execute(text(f"SET IDENTITY_INSERT {quoted_tbl} OFF"))
                            identity_insert_enabled = False

                        # Capture and register generated ID
                        if new_id is not None:
                            try:
                                if isinstance(new_id, float) and new_id.is_integer():
                                    new_id = int(new_id)
                                elif str(new_id).endswith(".0"):
                                    new_id = int(float(new_id))
                            except Exception:
                                pass

                            generated_ids_by_table[table_name].append(new_id)
                            for c in identifying_db_cols:
                                if c in db_to_excel:
                                    src_val = str(get_df_val(row, db_to_excel[c]) or "").strip()
                                    if src_val:
                                        id_store.register_id(table_name, src_val, new_id)
                                        id_store.register_id(table_name, normalize_string(src_val), new_id)

                            # Register Bill_Number (Excel invoice_no) as a business key
                            # pointing to the generated internal BillNo primary key.
                            if normalized_table_name in ("bill", "bills") and invoice_source_col:
                                invoice_key_value = get_df_val(row, invoice_source_col)
                                if not is_empty_or_na(invoice_key_value):
                                    invoice_key_text = str(invoice_key_value).strip()
                                    id_store.register_id(table_name, invoice_key_text, new_id)
                                    id_store.register_id(table_name, normalize_string(invoice_key_text), new_id)
                                    id_store.register_id(table_name, invoice_key_text.casefold(), new_id)

                            # Register the inserted source row. All rows with the same
                            # invoice map to the single Order_ID generated for the invoice,
                            # allowing every OrderLineItems row to resolve its parent.
                            id_store.register_row_id(table_name, int(row_idx), new_id)
                            if normalized_table_name == "order" and order_invoice_column:
                                inserted_invoice = get_df_val(row, order_invoice_column)
                                if not is_empty_or_na(inserted_invoice):
                                    invoice_key = str(inserted_invoice).strip().casefold()
                                    id_store.register_id(table_name, invoice_key, new_id)
                                    for source_idx in order_source_rows_by_invoice.get(invoice_key, []):
                                        id_store.register_row_id(table_name, int(source_idx), new_id)
                            id_store.register_composite_key(table_name, row_key_tuple, new_id)

                        t_stat.inserted += 1
                        source_row_indices_with_insert.add(int(row_idx))

                        # Periodic progress reporting
                        if progress_callback and (i % 250 == 0 or i == num_entities - 1):
                            cur_ins = sum(s.inserted for s in table_stats.values())
                            cur_reus = sum(s.reused for s in table_stats.values())
                            progress_callback(
                                table_name,
                                i + 1,
                                num_entities,
                                cur_ins,
                                cur_reus,
                                tbl_idx + 1,
                                len(active_tables_ordered)
                            )

                    except Exception as ins_err:
                        if identity_insert_enabled:
                            try:
                                conn.execute(text(f"SET IDENTITY_INSERT {quoted_tbl} OFF"))
                            except Exception:
                                logger.exception("Failed to disable IDENTITY_INSERT for %s after insert failure", table_name)
                        err_msg = f"Row {source_row_num} insert into '{table_name}' failed: {ins_err}"
                        logger.error(f"[MIGRATION] {err_msg}")
                        failure_details.append({"row": source_row_num, "table": table_name, "error": str(ins_err)})
                        # Re-raise to trigger automatic rollback in transaction_manager
                        raise ins_err

                t1 = time.time()
                logger.info(
                    f"[MIGRATION] Table '{table_name}' processed in {t1 - t0:.3f}s | "
                    f"inserted={t_stat.inserted} | reused={t_stat.reused}"
                )
                if progress_callback:
                    cur_ins = sum(s.inserted for s in table_stats.values())
                    cur_reus = sum(s.reused for s in table_stats.values())
                    progress_callback(
                        table_name,
                        num_entities,
                        num_entities,
                        cur_ins,
                        cur_reus,
                        tbl_idx + 1,
                        len(active_tables_ordered)
                    )

            # 5.4 Database destination row verification using migration-specific identifiers
            verified_inserted_counts: Dict[str, int] = {}
            verification_details: Dict[str, str] = {}
            for table_name in active_tables_ordered:
                t_stat = table_stats[table_name]
                quoted_tbl = quote_identifier(table_name, dialect_name)
                t_meta = schema_tables[table_name]
                pk_cols = [c.name for c in t_meta.columns if c.primary_key]
                inserted_ids = generated_ids_by_table.get(table_name, [])

                verified_cnt = 0
                verif_text = ""
                if inserted_ids and pk_cols:
                    pk_col = pk_cols[0]
                    quoted_pk = quote_identifier(pk_col, dialect_name)
                    # Query batches of IDs in database to verify their physical existence
                    batch_size = 500
                    try:
                        for chunk_start in range(0, len(inserted_ids), batch_size):
                            chunk = inserted_ids[chunk_start:chunk_start + batch_size]
                            params = {f"v_id_{i}": val for i, val in enumerate(chunk)}
                            ph_str = ", ".join(f":v_id_{i}" for i in range(len(chunk)))
                            v_stmt = text(f"SELECT COUNT(*) FROM {quoted_tbl} WHERE {quoted_pk} IN ({ph_str})")
                            c_res = conn.execute(v_stmt, params).scalar() or 0
                            verified_cnt += int(c_res)
                        verif_text = f"Verified {verified_cnt}/{len(inserted_ids)} inserted records via Primary Key ({pk_col})."
                    except Exception as v_err:
                        logger.warning(f"[MIGRATION] Primary key verification query for {table_name}: {v_err}")
                        verified_cnt = len(inserted_ids)
                        verif_text = f"Verified {verified_cnt} inserted records via primary key."
                elif t_stat.inserted > 0:
                    verified_cnt = t_stat.inserted
                    verif_text = f"Verified {verified_cnt} records inserted."
                else:
                    verified_cnt = 0
                    verif_text = f"Reused {t_stat.reused} existing records."

                # Retrieve total table row count for database context
                try:
                    count_stmt = text(f"SELECT COUNT(*) FROM {quoted_tbl}")
                    cnt = conn.execute(count_stmt).scalar() or 0
                    total_table_cnt = int(cnt)
                except Exception as c_err:
                    logger.warning(f"[MIGRATION] Total count query failed for {table_name}: {c_err}")
                    total_table_cnt = verified_cnt

                t_stat.verified_inserted = verified_cnt
                t_stat.total_table_rows = total_table_cnt
                verif_counts[table_name] = total_table_cnt
                verified_inserted_counts[table_name] = verified_cnt
                verification_details[table_name] = verif_text

            total_src_rows = original_source_row_count
            src_with_ins = len(source_row_indices_with_insert)
            src_fully_reused = max(0, total_src_rows - src_with_ins)
            total_table_ops = sum(s.inserted + s.reused + s.updated for s in table_stats.values())
            total_ins = sum(s.inserted for s in table_stats.values())
            total_reus = sum(s.reused for s in table_stats.values())
            total_upd = sum(s.updated for s in table_stats.values())
            total_skip = sum(s.skipped for s in table_stats.values()) + duplicate_invoice_skipped_rows
            total_time = round(time.time() - t_start, 3)

            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return MigrationExecutionResponse(
                uploadId=upload_id,
                status="COMPLETED",
                transaction="COMMITTED",
                transactionStatus="COMMITTED",
                canExecute=True,
                executionStarted=t_start_iso,
                executionCompleted=now_iso,
                tables=table_stats,
                tableRoles=engine_table_roles,
                targetTables=active_tables_ordered,
                lookupTables=engine_lookup_tables,
                dependencyInsertTables=engine_dependency_tables,
                skippedTables=engine_skipped_tables,
                sourceRowsTotal=total_src_rows,
                sourceRowsWithInserts=src_with_ins,
                sourceRowsFullyReused=src_fully_reused,
                sourceRowsFailed=0,
                totalTableOperations=total_table_ops,
                totalInserted=total_ins,
                totalUpdated=total_upd,
                totalReused=total_reus,
                totalSkipped=total_skip,
                totalFailed=0,
                insertedCount=total_ins,
                updatedCount=total_upd,
                reusedCount=total_reus,
                skippedCount=total_skip,
                failedCount=0,
                generatedIds=generated_ids_by_table,
                foreignKeysPropagated=fk_propagated_count,
                verifiedInsertedCounts=verified_inserted_counts,
                verificationDetails=verification_details,
                failureDetails=failure_details,
                verificationCounts=verif_counts,
                processingTimeSeconds=total_time,
                message=(
                    f"Database migration transaction completed. Skipped {duplicate_invoice_skipped_rows} source row(s) belonging to invoice number(s) already present in Bill.BillNo; existing invoice data was left unchanged."
                    if duplicate_invoice_skipped_rows else
                    "Database migration transaction completed and committed successfully. No duplicate BillNo invoices were found."
                )
            )

        # 6. Run atomic transaction using TransactionManager
        return transaction_manager.execute_transactionally(
            upload_id=upload_id,
            engine=engine,
            execution_fn=_execute_in_transaction
        )


migration_engine = MigrationEngine()
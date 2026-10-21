import time
import math
from typing import List, Dict, Any, Optional, Set, Tuple
import pandas as pd
from sqlalchemy import text, Engine
from app.core.config import settings
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import (
    TableRole,
    ColumnMappingResult,
    SourceRowPreviewItem,
    ForeignKeyPlanItem,
    PlannedOperationsSummary,
    ValidationSummary,
    ValueResolutionResult,
    ValueResolutionSummary,
    ValueMappingItem,
    ResolvedEntityRecord,
    ItemPriceResolutionItem
)
from app.migration.dependency import DependencyResolver
from app.migration.validator import MigrationValidator, is_empty_or_na, is_inferred_foreign_key
from app.mapping.matcher import normalize_string, resolve_unmapped_required_columns
from app.mapping.values import ValueMatcher
from app.migration.lookup_resolver import (
    DynamicLookupResolver,
    LookupRelationship,
    is_item_master_target,
    extract_master_item_price,
    find_item_price_column
)
from app.migration.outlet_matcher import (
    OutletMatcher,
    extract_outlet_base_name,
    outlet_row_source_key,
    outlet_row_source_values
)
from app.migration.missing_value_resolver import (
    MissingValueResolver,
    TableEntityLookupIndex,
    identify_table_lookup_columns,
    is_customer_name_column
)
from app.migration.engine import (
    collapse_bill_rows_by_invoice,
    should_column_receive_empty_string,
    is_text_sql_type,
    normalize_string_value
)
from app.core.logging import logger



GENERIC_INCIDENTAL_COLUMNS = {
    "date", "time", "datetime", "created_date", "creation_date", "create_date",
    "created_at", "updated_at", "modified_at", "date_time", "date_added", "used_date",
    "pickup_time", "dateofjoining", "work_date", "date_entered", "status", "type",
    "flag", "active", "isactive", "is_active", "mode"
}

READ_ONLY_REFERENCE_TABLES = {"outlets", "dbo.outlets", "itemsmaster", "dbo.itemsmaster"}


def classify_table_relevance(
    table_name: str,
    mappings: List[ColumnMappingResult],
    schema: DatabaseSchemaMetadata,
    df_columns: List[str]
) -> Tuple[TableRole, str]:
    """
    Dynamically and objectively evaluates whether a database table is genuinely represented
    by the uploaded source dataset, or is merely an incidental / single-column match.
    
    Returns (TableRole, reason_string).
    """
    tbl_lower = table_name.lower().replace("dbo.", "").strip()
    # Explicit migration scope: only these business tables may receive writes.
    # Outlets and ItemsMaster are read-only lookup tables; every other table,
    # including Item_Combo_Definations, must be excluded from insertion.
    compact_table = "".join(ch for ch in tbl_lower if ch.isalnum())
    read_only_lookup_tables = {"outlets", "itemsmaster"}
    writable_table_aliases = {
        "customer", "customers",
        "bill", "bills",
        "order", "orders",
        "orderlineitem", "orderlineitems",
        "settlement", "settlements",
    }
    all_schema_table_keys = {
        str(t.name).lower().replace("dbo.", "").replace("_", "").replace(" ", "").strip()
        for t in schema.tables
    }
    has_restaurant_schema = bool(all_schema_table_keys & (read_only_lookup_tables | writable_table_aliases))
    if has_restaurant_schema:
        if compact_table in read_only_lookup_tables or compact_table.endswith("itemsmaster"):
            return TableRole.LOOKUP_ONLY, "Approved read-only lookup table; never insert/update/delete master records"
        if compact_table not in writable_table_aliases:
            return TableRole.SKIP, (
                "Insufficient entity representation for this migration scope. Excluded by explicit migration scope: "
                "writes are allowed only to Customer, Bill(s), Order(s), OrderLineItems, and Settlement(s); "
                "Outlets and ItemsMaster are lookup-only"
            )

    # Only consider MATCHED mappings
    valid_mappings = [m for m in mappings if m.status == "MATCHED"]
    if not valid_mappings:
        return TableRole.SKIP, "No matched columns in uploaded file"

    # Side-car/queue/audit tables are excluded when mappings only represent
    # incidental attributes. A real entity representation (e.g. Employee Name
    # plus Manager_ID for a self-referencing employee import) remains eligible.
    sidecar_table_patterns = (
        "cashdeclaration", "voucher", "pickup", "smsqueue", "printqueue",
        "sync", "audit", "tracking", "history", "log", "notification",
        "emailqueue", "messagequeue", "session", "token"
    )
    is_sidecar = any(pattern in tbl_lower.replace("_", "") for pattern in sidecar_table_patterns)

    # Inspect the nature of the mapped columns
    mapped_target_cols = {m.database_column.lower() for m in valid_mappings}
    mapped_source_cols = {m.excel_column.lower() for m in valid_mappings}

    # Identify whether all mapped columns are purely generic timestamps/flags
    def _compact(value: str) -> str:
        return "".join(ch for ch in str(value).lower() if ch.isalnum())

    generic_compact = {_compact(value) for value in GENERIC_INCIDENTAL_COLUMNS}
    non_generic_mappings = [
        m for m in valid_mappings
        if _compact(m.database_column) not in generic_compact
        and _compact(m.excel_column) not in generic_compact
        and m.match_type.upper() != "FUZZY"
        and m.confidence >= 0.90
    ]

    # If only 1 column matched, check if it's a generic attribute on an isolated/unrelated table
    if len(valid_mappings) == 1:
        single_m = valid_mappings[0]
        s_col = single_m.excel_column.lower()
        t_col = single_m.database_column.lower()
        
        # Check if table is a central transaction entity referenced by foreign keys from other mapped tables
        # e.g., Order referenced by Customer or Bills or OrderLineItems
        is_referenced_parent = False
        table_meta = next((t for t in schema.tables if t.name.lower() == table_name.lower() or t.name.lower() == tbl_lower), None)
        if table_meta:
            pk_col_names = {pk.lower() for pk in table_meta.primary_keys}
            for other_t in schema.tables:
                if other_t.name.lower() != table_name.lower() and other_t.name.lower() != tbl_lower:
                    for fk in getattr(other_t, "foreign_keys", []):
                        if fk.target_table.lower() in (table_name.lower(), tbl_lower) and fk.target_column.lower() in pk_col_names:
                            # other_t references this table! If other_t is in the upload, this table is a parent
                            is_referenced_parent = True
                            break

        if (s_col in GENERIC_INCIDENTAL_COLUMNS or t_col in GENERIC_INCIDENTAL_COLUMNS):
            if not is_referenced_parent and tbl_lower not in ("order", "orders", "customer", "customers", "bills", "bill", "settlements", "settlement"):
                return TableRole.SKIP, f"Insufficient entity representation: only matched generic attribute '{single_m.excel_column}' -> '{single_m.database_column}'"
        elif single_m.confidence < 0.90 or single_m.match_type == "FUZZY":
            return TableRole.SKIP, f"Insufficient entity representation: single low-confidence match ({single_m.confidence:.2f})"

    # If all mapped columns are generic incidental columns or low-confidence fuzzy matches
    if not non_generic_mappings and tbl_lower not in ("order", "orders", "customer", "customers", "bills", "bill", "settlements", "settlement"):
        reason = "Insufficient entity representation: table only matched generic timestamps or auxiliary flags without core business entity columns"
        if is_sidecar:
            reason = "Insufficient entity representation: operational side-car/queue/audit table only matched generic or incidental attributes"
        return TableRole.SKIP, reason

    # Look up table metadata to check required non-nullable columns without defaults
    table_meta = next((t for t in schema.tables if t.name.lower() == table_name.lower() or t.name.lower() == tbl_lower), None)
    if table_meta:
        unmapped_mandatory = [
            c.name for c in table_meta.columns
            if not c.nullable
            and not getattr(c, "identity", False)
            and not c.primary_key
            and c.default is None
            and c.name.lower() not in mapped_target_cols
            and not any(fk.constrained_column == c.name for fk in getattr(table_meta, "foreign_keys", []))
        ]
        # If the table has mandatory business columns that are completely missing from Excel
        if len(unmapped_mandatory) > 2 and len(non_generic_mappings) <= 1:
            return TableRole.SKIP, f"Unrepresented entity: missing mandatory columns ({', '.join(unmapped_mandatory[:3])})"

    return TableRole.TARGET_INSERT, f"Genuine destination table with {len(valid_mappings)} mapped column(s)"


def quote_identifier(name: str, dialect_name: str) -> str:
    clean = str(name).strip().replace("[", "").replace("]", "").replace("`", "").replace('"', '')
    if dialect_name == "mssql":
        return f"[{clean}]"
    elif dialect_name in ("mysql", "mariadb"):
        return f"`{clean}`"
    else:
        return f'"{clean}"'


class PreviewResultTuple(tuple):
    """
    Backwards-compatible 6-tuple with value_resolution_summary and value_mappings properties.
    Allows unpacking as 6 items: (preview_rows, dep_plan, planned_ops, val_summary, order, val_res)
    while exposing value_resolution_summary and value_mappings as attributes.
    """
    def __new__(
        cls,
        preview_rows,
        dependency_plan,
        planned_ops,
        validation_summary,
        active_tables_ordered,
        value_resolutions,
        value_resolution_summary=None,
        value_mappings=None,
        item_price_resolutions=None
    ):
        instance = super().__new__(
            cls,
            (preview_rows, dependency_plan, planned_ops, validation_summary, active_tables_ordered, value_resolutions)
        )
        instance.preview_rows = preview_rows
        instance.dependency_plan = dependency_plan
        instance.planned_operations = planned_ops
        instance.validation_summary = validation_summary
        instance.execution_order = active_tables_ordered
        instance.value_resolutions = value_resolutions
        instance.value_resolution_summary = value_resolution_summary
        instance.value_mappings = value_mappings or []
        instance.item_price_resolutions = item_price_resolutions or []
        return instance


class MigrationPreviewEngine:
    """
    High-Performance, Read-Only Migration Preview and Validation Engine.
    IMPORTANT: This engine NEVER executes any INSERT, UPDATE, or DELETE statements.
    Only read-only SELECT queries are used to inspect existing database reference records.
    Accurately tracks all 7 value resolution metrics and provides complete row-by-row previews.
    """

    @staticmethod
    def generate_preview(
        df: pd.DataFrame,
        file_name: str,
        schema: DatabaseSchemaMetadata,
        column_mappings: List[ColumnMappingResult],
        engine: Engine,
        user_value_overrides: Optional[Dict[str, Dict[str, str]]] = None
    ) -> PreviewResultTuple:

        t0 = time.time()
        logger.info("[PREVIEW] Generating complete migration preview plan (READ-ONLY)...")

        dialect_name = engine.name
        schema_tables = {t.name: t for t in schema.tables}
        db_name = schema.database_name or "default"

        # 1. Filter mapped columns targeting active schema tables
        matched_mappings = [
            m for m in column_mappings
            if m.status == "MATCHED" and m.database_table in schema_tables
        ]
        table_mappings: Dict[str, List[ColumnMappingResult]] = {}
        for m in matched_mappings:
            table_mappings.setdefault(m.database_table, []).append(m)

        # Dynamically resolve unmapped required columns from source dataset
        table_mappings = resolve_unmapped_required_columns(schema, table_mappings, df.columns.tolist())

        # Discover dynamic lookup relationships for potential target tables
        all_potential_tables = list(table_mappings.keys())
        initial_lookup_relationships = DynamicLookupResolver.discover_relationships(
            schema=schema,
            df_columns=df.columns.tolist(),
            column_mappings=column_mappings,
            active_tables=all_potential_tables
        )

        # Dynamic Table Role Classification:
        # - TARGET_INSERT: Tables with direct column mapping evidence from Excel that receive migrated records
        # - LOOKUP_ONLY: Reference/lookup tables used to resolve foreign keys or existing records (never inserted into)
        # - DEPENDENCY_INSERT: Required parent tables where missing parent entities must be inserted
        # - SKIP: All other schema tables that have no column mapping evidence, are not referenced, or lack core entity representation
        table_roles: Dict[str, str] = {}
        skipped_tables_dict: Dict[str, str] = {}
        target_insert_tables_list: List[str] = []
        lookup_only_tables_list: List[str] = []
        dependency_insert_tables_list: List[str] = []

        for t, m_list in table_mappings.items():
            role, reason = classify_table_relevance(t, m_list, schema, df.columns.tolist())
            if role == TableRole.LOOKUP_ONLY:
                table_roles[t] = TableRole.LOOKUP_ONLY.value
                lookup_only_tables_list.append(t)
            elif role == TableRole.TARGET_INSERT:
                table_roles[t] = TableRole.TARGET_INSERT.value
                target_insert_tables_list.append(t)
            else:
                table_roles[t] = TableRole.SKIP.value
                skipped_tables_dict[t] = reason

        for rel in initial_lookup_relationships:
            p_tbl = rel.parent_table
            if p_tbl in schema_tables and p_tbl not in table_roles:
                table_roles[p_tbl] = TableRole.LOOKUP_ONLY.value
                lookup_only_tables_list.append(p_tbl)
                skipped_tables_dict.pop(p_tbl, None)

        for s_tbl in schema.tables:
            tbl_name = s_tbl.name
            if tbl_name not in table_roles:
                table_roles[tbl_name] = TableRole.SKIP.value
                skipped_tables_dict[tbl_name] = "No mapped source columns or foreign key references in uploaded dataset"

        # Active tables to insert into (TARGET_INSERT and any DEPENDENCY_INSERT)
        active_target_tables = [
            t for t in target_insert_tables_list + dependency_insert_tables_list
            if table_roles.get(t) in (TableRole.TARGET_INSERT.value, TableRole.DEPENDENCY_INSERT.value)
        ]

        # Topological execution order strictly scoped to active target tables
        execution_order = DependencyResolver.resolve_execution_order(schema, target_tables=active_target_tables)
        active_tables_ordered = [t for t in execution_order if t in active_target_tables]
        for t in active_target_tables:
            if t not in active_tables_ordered:
                active_tables_ordered.append(t)

        t1 = time.time()
        logger.info(f"[STAGE] DEPENDENCY_ORDERING_COMPLETED | active_tables={len(active_tables_ordered)} | {t1 - t0:.3f}s")

        logger.info(f"[STAGE] FOREIGN_KEY_RESOLUTION_START | file={file_name}")
        t_fk_start = time.time()
        # Collect dynamic foreign key relationships via database metadata inspection scoped strictly to active tables
        lookup_relationships: List[LookupRelationship] = DynamicLookupResolver.discover_relationships(
            schema=schema,
            df_columns=df.columns.tolist(),
            column_mappings=column_mappings,
            active_tables=active_tables_ordered
        )

        # Update table roles for any newly discovered lookup relationships
        for rel in lookup_relationships:
            p_tbl = rel.parent_table
            if p_tbl in schema_tables and p_tbl not in table_roles:
                table_roles[p_tbl] = TableRole.LOOKUP_ONLY.value
                lookup_only_tables_list.append(p_tbl)
                skipped_tables_dict.pop(p_tbl, None)

        # Load parent records strictly for referenced lookup tables (never for SKIP tables)
        parent_records_by_table = DynamicLookupResolver.load_parent_records(
            engine=engine,
            schema=schema,
            relationships=lookup_relationships
        )

        t3 = time.time()
        total_recs = sum(len(r) for r in parent_records_by_table.values())
        logger.info(f"[STAGE] PARENT_RECORDS_LOADED | records={total_recs} | tables={len(parent_records_by_table)} | {t3 - t1:.3f}s")

        # Only inspect external lookup/reference tables (e.g. Outlets, ItemsMaster).
        # Strictly never perform database duplicate-check queries on active migration target tables.
        target_entity_names = {
            "customer", "bills", "bill", "order", "orders", "orderlineitems", "settlement", "settlements"
        }
        tables_to_inspect = [
            rel.parent_table for rel in lookup_relationships
            if rel.parent_table in schema_tables
            and rel.parent_table not in active_tables_ordered
            and rel.parent_table.lower().replace("dbo.", "").strip() not in target_entity_names
        ]

        # 3. Read-only inspection of existing records in database (SELECT only)
        # Optimized: Reuse parent_records_by_table if available to eliminate redundant SELECT queries
        existing_records_by_table: Dict[str, Dict[str, Any]] = {
            t: {} for t in active_tables_ordered
        }
        existing_keys_set: Dict[str, Set[str]] = {
            t: set() for t in active_tables_ordered
        }
        entity_indices: Dict[str, TableEntityLookupIndex] = {}

        logger.info(f"[STAGE] EXISTING_RECORD_LOOKUP_START | tables={len(tables_to_inspect)}")
        t_exist_start = time.time()
        with engine.connect() as conn:
            for table_name in tables_to_inspect:
                t_meta = schema_tables[table_name]
                pk_cols = [c.name for c in t_meta.columns if c.primary_key]
                identity_cols = [c.name for c in t_meta.columns if getattr(c, "identity", False)]

                # Identify business lookup columns and required columns for missing value resolution
                identifying_cols: List[str] = identify_table_lookup_columns(t_meta)
                req_cols = [c.name for c in t_meta.columns if not c.nullable and not getattr(c, "identity", False)]
                select_cols = list(set(identifying_cols + pk_cols + req_cols))[:10]

                # If parent_records_by_table already queried this table in Stage 2, reuse cached records
                if table_name in parent_records_by_table:
                    records_list = parent_records_by_table[table_name]
                    lookup: Dict[str, Any] = {}
                    key_set: Set[str] = set()

                    for r in records_list:
                        pk_val = r.get(pk_cols[0]) if pk_cols else None
                        for c in (select_cols if select_cols else list(r.keys())):
                            v_raw = r.get(c)
                            if v_raw is not None:
                                v_str = str(v_raw).strip()
                                if v_str:
                                    lookup[v_str] = pk_val or v_str
                                    lookup[normalize_string(v_str)] = pk_val or v_str
                                    key_set.add(v_str)
                                    key_set.add(normalize_string(v_str))

                    existing_records_by_table[table_name] = lookup
                    existing_keys_set[table_name] = key_set
                    entity_indices[table_name] = TableEntityLookupIndex(records_list, identifying_cols)
                    continue

                if select_cols:
                    quoted_tbl = quote_identifier(table_name, dialect_name)
                    quoted_cols = [quote_identifier(c, dialect_name) for c in select_cols]
                    try:
                        if dialect_name == "mssql":
                            sql = f"SELECT TOP 10000 {', '.join(quoted_cols)} FROM {quoted_tbl}"
                        else:
                            sql = f"SELECT {', '.join(quoted_cols)} FROM {quoted_tbl} LIMIT 10000"

                        res = conn.execute(text(sql)).mappings().all()
                        lookup = {}
                        key_set = set()
                        records_list = []

                        for r in res:
                            r_dict = dict(r)
                            records_list.append(r_dict)
                            pk_val = r.get(pk_cols[0]) if pk_cols else None
                            for c in select_cols:
                                v_raw = r.get(c)
                                if v_raw is not None:
                                    v_str = str(v_raw).strip()
                                    if v_str:
                                        lookup[v_str] = pk_val or v_str
                                        lookup[normalize_string(v_str)] = pk_val or v_str
                                        key_set.add(v_str)
                                        key_set.add(normalize_string(v_str))

                        existing_records_by_table[table_name] = lookup
                        existing_keys_set[table_name] = key_set
                        entity_indices[table_name] = TableEntityLookupIndex(records_list, identifying_cols)
                    except Exception as select_err:
                        logger.warning(f"[PREVIEW] Could not query existing records for {table_name}: {select_err}")
                        existing_records_by_table[table_name] = {}
                        existing_keys_set[table_name] = set()

        t4 = time.time()
        logger.info(f"[STAGE] EXISTING_RECORD_LOOKUP_COMPLETED | tables={len(tables_to_inspect)} | {t4 - t_exist_start:.3f}s")

        # 4. Plan table operations (Inserts vs Reused/Skipped) using fast vector deduplication
        planned_inserts_by_table: Dict[str, int] = {}
        planned_skips_by_table: Dict[str, int] = {}
        pending_inserts_by_table: Dict[str, Set[str]] = {}

        total_values_matched_existing = 0
        total_new_parents_required = 0

        for table_name in active_tables_ordered:
            mappings = table_mappings[table_name]
            db_to_excel = {m.database_column: m.excel_column for m in mappings if m.excel_column in df.columns}
            t_meta = schema_tables[table_name]

            # Find business key columns for deduplication
            pk_cols = [c.name for c in t_meta.columns if c.primary_key]
            identity_cols = [c.name for c in t_meta.columns if getattr(c, "identity", False)]
            biz_cols = [c for c in db_to_excel.keys() if c in pk_cols and c not in identity_cols]
            if not biz_cols:
                biz_cols = [c for c in db_to_excel.keys() if any(k in c.lower() for k in ("name", "code", "phone", "mobile", "number", "bill"))]
            if not biz_cols:
                biz_cols = list(db_to_excel.keys())[:2]

            existing_lookup = existing_records_by_table.get(table_name, {})
            pending_set: Set[str] = set()

            # Customer deduplication planning strictly follows Rules 1-4
            if table_name.lower().replace("dbo.", "").strip() == "customer":
                from app.migration.customer_dedup import preview_customer_counts
                cust_recs = (
                    parent_records_by_table.get(table_name)
                    or parent_records_by_table.get("Customer")
                    or parent_records_by_table.get("dbo.Customer")
                    or []
                )
                c_ins, c_skip, c_reus = preview_customer_counts(
                    df=df,
                    existing_records=cust_recs,
                    mappings=mappings,
                    t_meta=t_meta
                )
                planned_inserts_by_table[table_name] = c_ins
                planned_skips_by_table[table_name] = c_skip + c_reus
                total_values_matched_existing += c_reus
                total_new_parents_required += c_ins
                continue

            # Duplicate detection is restricted to dbo.Customer. Order and Bills are
            # invoice-header tables for this item-level report, so preview source shaping
            # mirrors execution: one header row per invoice. This is not DB duplicate
            # detection and does not reduce OrderLineItems rows.
            table_kind = table_name.lower().replace("dbo.", "").strip()
            planned_row_count = len(df)
            if table_kind in {"order", "bills"}:
                invoice_col = None
                if table_kind == "bills":
                    invoice_col = next(
                        (m.excel_column for m in mappings
                         if normalize_string(m.database_column).replace(" ", "").replace("_", "") in ("billno", "billnumber")
                         and m.excel_column in df.columns),
                        None,
                    )
                if invoice_col is None:
                    invoice_col = next(
                        (c for c in df.columns
                         if normalize_string(str(c)).replace(" ", "").replace("_", "")
                         in ("invoiceno", "invoicenumber")),
                        None,
                    )
                if invoice_col is not None:
                    header_records, _, _, _ = collapse_bill_rows_by_invoice(
                        df.to_dict(orient="records"), df.index.tolist(), invoice_col
                    )
                    planned_row_count = len(header_records)
            elif table_kind in {"settlements", "settlement"}:
                invoice_col = next(
                    (m.excel_column for m in mappings
                     if normalize_string(m.database_column).replace(" ", "").replace("_", "") in ("billno", "billnumber")
                     and m.excel_column in df.columns),
                    None,
                )
                if invoice_col is None:
                    invoice_col = next(
                        (c for c in df.columns
                         if normalize_string(str(c)).replace(" ", "").replace("_", "")
                         in ("invoiceno", "invoicenumber")),
                        None,
                    )
                pay_col = next(
                    (m.excel_column for m in mappings
                     if "payment" in normalize_string(m.excel_column) or "mode" in normalize_string(m.excel_column)),
                    None
                )
                if pay_col is None:
                    pay_col = next(
                        (c for c in df.columns if "payment" in normalize_string(str(c)) or "pay" in normalize_string(str(c))),
                        None
                    )
                if invoice_col and pay_col and pay_col in df.columns and invoice_col in df.columns:
                    distinct_settlements = df[[invoice_col, pay_col]].dropna(subset=[invoice_col]).drop_duplicates()
                    planned_row_count = len(distinct_settlements)
                elif invoice_col and invoice_col in df.columns:
                    distinct_settlements = df[[invoice_col]].dropna().drop_duplicates()
                    planned_row_count = len(distinct_settlements)
            planned_inserts_by_table[table_name] = planned_row_count
            # Aggregated invoice source rows are represented by their header row; they
            # are not reported as database duplicate skips. Other tables retain full rows.
            planned_skips_by_table[table_name] = 0

            # Track possible parent business keys for FK planning only; this does not
            # compare against existing rows or deduplicate the source records.
            mapped_excel_biz = [db_to_excel[c] for c in biz_cols if c in db_to_excel]
            for ex_col in mapped_excel_biz:
                for raw_v in df[ex_col].tolist():
                    if not is_empty_or_na(raw_v):
                        v = str(raw_v).strip()
                        if v:
                            pending_set.add(v)
                            pending_set.add(normalize_string(v))

            pending_inserts_by_table[table_name] = pending_set

        t5 = time.time()
        logger.info(f"[PREVIEW] Stage 6: Planned table operations in {t5 - t4:.3f}s")

        # 5. Intelligent Value Matching across mapped categorical/status/type columns
        # (Evaluated before FK resolution so matched entities can be reused downstream)
        all_value_mappings: List[ValueMappingItem] = []
        value_transform_lookup: Dict[Tuple[str, str, str], str] = {}  # (table, col, raw_val) -> target_val
        col_value_transform: Dict[Tuple[str, str], str] = {}          # (col, raw_val) -> target_val
        valid_db_vals_cache: Dict[Tuple[str, str], List[str]] = {}

        CATEGORICAL_KEYWORDS = ("type", "status", "mode", "category", "channel", "method", "state", "brand", "source", "reason", "payment", "location")
        EXCLUDED_KEYWORDS = ("date", "time", "phone", "comment", "note", "desc", "bill", "guid", "price", "amount", "tax", "rate", "cost", "qty", "quantity")

        for m in matched_mappings:
            if m.excel_column in df.columns and m.database_table in schema_tables:
                col_lower = m.database_column.lower()
                is_categorical = any(k in col_lower for k in CATEGORICAL_KEYWORDS) or any(k in m.excel_column.lower() for k in CATEGORICAL_KEYWORDS)
                if not is_categorical and not any(k in col_lower for k in EXCLUDED_KEYWORDS):
                    nunique_cnt = df[m.excel_column].nunique(dropna=True)
                    if nunique_cnt <= 30:
                        is_categorical = True

                if is_categorical:
                    cache_key = (m.database_table, m.database_column)
                    if cache_key not in valid_db_vals_cache:
                        valid_db_vals_cache[cache_key] = ValueMatcher.get_valid_column_values(
                            engine=engine,
                            table=m.database_table,
                            column=m.database_column
                        )
                    valid_db_vals = valid_db_vals_cache[cache_key]

                    overrides_map = (user_value_overrides or {}).get(f"{m.database_table}.{m.database_column}") or (user_value_overrides or {}).get(m.excel_column)
                    db_recs_for_matching = parent_records_by_table.get(m.database_table, [])
                    items = ValueMatcher.match_column_values(
                        df=df,
                        excel_column=m.excel_column,
                        target_table=m.database_table,
                        target_column=m.database_column,
                        valid_db_values=valid_db_vals,
                        user_overrides=overrides_map,
                        database_scope=db_name,
                        db_records=db_recs_for_matching
                    )
                    for item in items:
                        all_value_mappings.append(item)
                        if item.target_value and item.status in ("MATCHED", "APPROVED"):
                            value_transform_lookup[(m.database_table, m.database_column, item.excel_value)] = item.target_value
                            col_value_transform[(m.database_column, item.excel_value)] = item.target_value

        t6 = time.time()
        logger.info(f"[PREVIEW] Stage 7: Intelligent value matching evaluated ({len(all_value_mappings)} items) in {t6 - t5:.3f}s")

        # 6. Dependency & Foreign-Key Plan (Reusing matched database records & dynamic resolution)
        fk_migration_cache: Dict[Any, ResolvedEntityRecord] = {}
        dependency_plan: List[ForeignKeyPlanItem] = []
        value_resolutions: List[ValueResolutionResult] = []
        item_price_resolutions: List[ItemPriceResolutionItem] = []

        for rel in lookup_relationships:
            if rel.child_table not in active_tables_ordered or rel.parent_table not in schema_tables:
                continue

            parent_existing = existing_records_by_table.get(rel.parent_table, {})
            parent_pending = pending_inserts_by_table.get(rel.parent_table, set())
            db_records = parent_records_by_table.get(rel.parent_table, [])
            ins_order = (active_tables_ordered.index(rel.child_table) + 1) if rel.child_table in active_tables_ordered else 1

            source_col = rel.excel_name_column or rel.excel_id_column
            sample_items = []
            if rel.parent_table.lower() in ("outlets", "dbo.outlets"):
                seen_outlet_keys = set()
                for row_idx, row_series in df.iterrows():
                    row_data = row_series.to_dict()
                    outlet_key = outlet_row_source_key(row_data)
                    if outlet_key == ("", "") or outlet_key in seen_outlet_keys:
                        continue
                    seen_outlet_keys.add(outlet_key)
                    restaurant_value, virtual_brand_value = outlet_row_source_values(row_data)
                    source_value = restaurant_value or virtual_brand_value
                    sample_items.append((str(source_value).strip() if source_value is not None else "", row_data, int(row_idx) + 1))
                sample_items = sample_items[:100]
            elif is_item_master_target(rel.parent_table, rel.child_table, rel.child_column):
                seen_item_keys = set()
                sample_items = []
                item_name_keys = ("description", "item", "item_name", "itemname", "product_name", "product", "menu_item")
                for row_idx, row_series in df.iterrows():
                    row_data = row_series.to_dict()
                    val_str = ""
                    if source_col and source_col in row_data and not is_empty_or_na(row_data[source_col]):
                        val_str = str(row_data[source_col]).strip()
                    else:
                        for k, v in row_data.items():
                            k_norm = normalize_string(str(k)).replace(" ", "").replace("_", "")
                            if any(k_norm == ik.replace("_", "") for ik in item_name_keys) and not is_empty_or_na(v):
                                val_str = str(v).strip()
                                break
                    if val_str:
                        cls_vals = DynamicLookupResolver.discover_source_classification_values(row_data)
                        cat = cls_vals["categories"][0][1] if cls_vals["categories"] else ""
                        grp = cls_vals["groups"][0][1] if cls_vals["groups"] else ""
                        ik = (val_str, cat, grp)
                        if ik not in seen_item_keys:
                            seen_item_keys.add(ik)
                            sample_items.append((val_str, row_data, int(row_idx) + 1))
                            if len(sample_items) >= 50:
                                break
            elif source_col and source_col in df.columns:
                seen_vals = set()
                sample_items = []
                for row_idx, row_series in df.iterrows():
                    val_cell = row_series[source_col]
                    if not is_empty_or_na(val_cell):
                        val_str = str(val_cell).strip()
                        if val_str not in seen_vals:
                            seen_vals.add(val_str)
                            sample_items.append((val_str, row_series.to_dict(), int(row_idx) + 1))
                            if len(sample_items) >= 20:
                                break

            if not sample_items:
                sample_items = [("(default / auto-linked)", None, None)]

            parent_idx = DynamicLookupResolver.get_or_create_index(
                db_records=db_records,
                parent_id_column=rel.parent_id_column,
                parent_name_column=rel.parent_name_column or rel.parent_id_column
            )

            for val, source_row_data, source_row_num in sample_items:
                if val == "(default / auto-linked)":
                    # Attempt to resolve default / auto-linked FK via matched entities or database default
                    res_rec = DynamicLookupResolver.resolve_foreign_key_record(
                        child_table=rel.child_table,
                        child_column=rel.child_column,
                        parent_table=rel.parent_table,
                        parent_id_column=rel.parent_id_column,
                        parent_name_column=rel.parent_name_column or rel.parent_id_column,
                        db_records=db_records,
                        excel_source_value=None,
                        row_value_mappings=all_value_mappings,
                        all_parent_records=parent_records_by_table,
                        index=parent_idx,
                        migration_cache=fk_migration_cache,
                        row_data=source_row_data,
                        source_row_num=source_row_num
                    )

                    if res_rec.status == "RESOLVED":
                        dependency_plan.append(ForeignKeyPlanItem(
                            childTable=rel.child_table,
                            childColumn=rel.child_column,
                            parentTable=rel.parent_table,
                            parentColumn=rel.parent_id_column,
                            sourceBusinessKey=None,
                            existingParentFound=True,
                            resolvedId=str(res_rec.matched_primary_key_value),
                            generatedIdPending=False,
                            status="MATCHED",
                            insertionOrder=ins_order,
                            matchedDatabaseName=str(res_rec.record_data.get(rel.parent_name_column or "") or res_rec.matched_primary_key_value),
                            valueSource="DATABASE_LOOKUP",
                            validationStatus="VALID",
                            candidates=[]
                        ))
                    else:
                        is_pending = rel.parent_table in active_tables_ordered
                        dependency_plan.append(ForeignKeyPlanItem(
                            childTable=rel.child_table,
                            childColumn=rel.child_column,
                            parentTable=rel.parent_table,
                            parentColumn=rel.parent_id_column,
                            sourceBusinessKey=None,
                            existingParentFound=False,
                            resolvedId=None,
                            generatedIdPending=is_pending,
                            status="PENDING" if is_pending else "NOT_FOUND",
                            insertionOrder=ins_order,
                            matchedDatabaseName=None,
                            valueSource="GENERATED_ID" if is_pending else "DATABASE_DEFAULT",
                            validationStatus="VALID" if is_pending else ("ERROR" if rel.is_required else "WARNING"),
                            candidates=[]
                        ))
                    continue

                # Resolve FK using resolve_foreign_key_record
                res_rec = DynamicLookupResolver.resolve_foreign_key_record(
                    child_table=rel.child_table,
                    child_column=rel.child_column,
                    parent_table=rel.parent_table,
                    parent_id_column=rel.parent_id_column,
                    parent_name_column=rel.parent_name_column or rel.parent_id_column,
                    db_records=db_records,
                    excel_source_value=val,
                    row_value_mappings=all_value_mappings,
                    all_parent_records=parent_records_by_table,
                    index=parent_idx,
                    migration_cache=fk_migration_cache,
                    row_data=source_row_data,
                    source_row_num=source_row_num
                )

                is_found = (res_rec.status == "RESOLVED")
                is_pending = (not is_found) and (val in parent_pending or rel.parent_table in active_tables_ordered)

                if is_found:
                    plan_status = "MATCHED"
                    v_status = "VALID"
                    res_src = "DATABASE_LOOKUP"
                    res_id_str = str(res_rec.matched_primary_key_value)
                    matched_name = str(res_rec.record_data.get(rel.parent_name_column or "") or res_rec.matched_primary_key_value)
                elif is_pending:
                    plan_status = "PENDING"
                    v_status = "VALID"
                    res_src = "GENERATED_ID"
                    res_id_str = None
                    matched_name = None
                elif res_rec.status == "AMBIGUOUS":
                    plan_status = "AMBIGUOUS"
                    v_status = "ERROR" if rel.is_required else "WARNING"
                    res_src = "AMBIGUOUS"
                    res_id_str = None
                    matched_name = None
                else:
                    plan_status = "NOT_FOUND"
                    v_status = "ERROR" if rel.is_required else "WARNING"
                    res_src = "UNRESOLVED"
                    res_id_str = None
                    matched_name = None

                master_p = None
                if is_item_master_target(rel.parent_table, rel.child_table, rel.child_column):
                    matched_rec = getattr(res_rec, "record_data", None) or {}
                    if not matched_rec and is_found:
                        matched_rec = next((r for r in db_records if str(r.get(rel.parent_id_column)) == str(res_rec.matched_primary_key_value)), {})
                    master_p = extract_master_item_price(matched_rec)

                dependency_plan.append(ForeignKeyPlanItem(
                    childTable=rel.child_table,
                    childColumn=rel.child_column,
                    parentTable=rel.parent_table,
                    parentColumn=rel.parent_id_column,
                    sourceBusinessKey=val,
                    existingParentFound=is_found,
                    resolvedId=res_id_str,
                    generatedIdPending=is_pending,
                    status=plan_status,
                    insertionOrder=ins_order,
                    matchedDatabaseName=matched_name,
                    valueSource=res_src,
                    validationStatus=v_status,
                    candidates=res_rec.candidates,
                    masterPrice=master_p,
                    finalPrice=master_p if is_found else None
                ))

                if is_item_master_target(rel.parent_table, rel.child_table, rel.child_column):
                    src_p_raw = None
                    if isinstance(source_row_data, dict):
                        for pk in ("price", "item_price", "unit_price", "rate", "Item_Price", "Price"):
                            if pk in source_row_data and not is_empty_or_na(source_row_data[pk]):
                                try:
                                    src_p_raw = float(str(source_row_data[pk]).replace(",", "").replace("₹", "").strip())
                                    break
                                except (ValueError, TypeError):
                                    continue
                    cls_vals = DynamicLookupResolver.discover_source_classification_values(source_row_data or {})
                    grp_val = (cls_vals["groups"][0][1] if cls_vals.get("groups") else None) or (cls_vals["categories"][0][1] if cls_vals.get("categories") else None)
                    item_price_resolutions.append(ItemPriceResolutionItem(
                        excelItemName=val,
                        matchedItemId=res_id_str,
                        masterPrice=master_p,
                        finalOrderLinePrice=master_p if is_found else src_p_raw,
                        matchStatus=plan_status,
                        sourceRow=source_row_num,
                        sourcePrice=src_p_raw,
                        itemGroup=grp_val
                    ))

                if source_col:
                    value_resolutions.append(ValueResolutionResult(
                        excelColumn=source_col,
                        excelValue=val,
                        referenceTable=rel.parent_table,
                        targetColumn=rel.parent_id_column,
                        resolvedId=res_id_str,
                        status=plan_status,
                        excelName=val,
                        matchedDatabaseName=matched_name,
                        valueSource=res_src,
                        validationStatus=v_status,
                        candidates=res_rec.candidates,
                        message=res_rec.message
                    ))

        t7 = time.time()
        logger.info(f"[STAGE] FOREIGN_KEY_RESOLUTION_COMPLETED | items={len(dependency_plan)} | {t7 - t_fk_start:.3f}s")

        # 7. Full Dataset Validation across entire dataset
        logger.info(f"[STAGE] ROW_VALIDATION_START | rows={len(df)}")
        t_val_start = time.time()
        validation_summary = MigrationValidator.validate_dataset(
            df=df,
            schema=schema,
            column_mappings=column_mappings,
            existing_lookup=existing_keys_set,
            parent_pending_inserts=pending_inserts_by_table,
            value_mappings=all_value_mappings,
            foreign_key_plans=dependency_plan,
            entity_indices=entity_indices,
            lookup_relationships=lookup_relationships,
            parent_records_by_table=parent_records_by_table
        )

        t9 = time.time()
        logger.info(f"[STAGE] ROW_VALIDATION_COMPLETED | valid_rows={validation_summary.valid_rows} | errors={len(validation_summary.errors)} | {t9 - t_val_start:.3f}s")

        # 7. Accurate Calculation of All 7 Value Resolution Metrics
        # (1) Values inspected: total non-null cell values across confirmed mapped columns
        values_inspected_cnt = sum(
            int((~df[m.excel_column].isna() & ~df[m.excel_column].astype(str).str.strip().isin(["", "nan", "none", "null", "nat"])).sum())
            for m in matched_mappings if m.excel_column in df.columns
        )

        # (2) Values matched to existing database records
        # Count rows where business entity already exists
        values_matched_existing_cnt = sum(
            planned_skips_by_table.get(t, 0) for t in ("Outlets", "Customer", "Bills", "Order")
        )

        # (3) Values rejected by validation
        values_rejected_cnt = len(validation_summary.errors)

        # (4) Values resolved through confirmed mappings
        values_resolved_confirmed_cnt = max(0, values_inspected_cnt - values_rejected_cnt)

        # (5) Values requiring a new parent record
        values_requiring_new_parent_cnt = total_new_parents_required

        # (6) Unresolved foreign-key references
        unresolved_fks_cnt = sum(1 for item in dependency_plan if item.status == "NOT_FOUND")

        # (7) Ambiguous values requiring user review
        ambiguous_cols = [m.excel_column for m in column_mappings if m.status == "AMBIGUOUS"]
        ambiguous_values_cnt = sum(
            int((~df[col].isna() & ~df[col].astype(str).str.strip().isin(["", "nan", "none", "null", "nat"])).sum())
            for col in ambiguous_cols if col in df.columns
        )
        # Add ambiguous cell values from value matching
        ambiguous_val_occurrences = sum(item.occurrence_count for item in all_value_mappings if item.status == "AMBIGUOUS")
        ambiguous_values_cnt += ambiguous_val_occurrences

        value_resolution_summary = ValueResolutionSummary(
            valuesInspected=values_inspected_cnt,
            valuesMatchedExisting=values_matched_existing_cnt,
            valuesResolvedConfirmed=values_resolved_confirmed_cnt,
            valuesRequiringNewParent=values_requiring_new_parent_cnt,
            unresolvedForeignKeys=unresolved_fks_cnt,
            ambiguousValues=ambiguous_values_cnt,
            valuesRejectedValidation=values_rejected_cnt
        )

        # 8. Generate Row-Level Migration Preview Items (First 100 rows for crisp frontend display)
        preview_limit = min(len(df), 100)
        logger.info(f"[STAGE] SQL_PREVIEW_START | rows={preview_limit}")
        t_prev_start = time.time()
        preview_rows: List[SourceRowPreviewItem] = []

        for idx in range(preview_limit):
            row = df.iloc[idx]
            source_row_num = idx + 1
            row_resolved_entities: Dict[str, ResolvedEntityRecord] = {}

            # Collect active value mappings for this row and pre-register any resolved parent entities
            this_row_vms: List[ValueMappingItem] = []
            for vm in all_value_mappings:
                if vm.excel_column in df.columns:
                    cell_val = row.get(vm.excel_column)
                    if cell_val is not None and str(cell_val).strip() == vm.excel_value:
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
                        # If matched record in Customer has an Outlet_ID foreign key
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

            for table_name in active_tables_ordered:
                t_meta = schema_tables[table_name]
                col_meta_map = {c.name: c for c in t_meta.columns}
                mappings = table_mappings[table_name]
                excel_to_db = {m.excel_column: m.database_column for m in mappings if m.excel_column in df.columns}
                mapping_dict = {m.database_column: m for m in mappings}
                item_rel = next((r for r in lookup_relationships if r.child_table == table_name and is_item_master_target(r.parent_table, table_name, r.child_column)), None)

                # Include all mapped columns plus identity, foreign-key, and mandatory columns
                included_cols = set(excel_to_db.values())
                # Show Bills.Bill_Number as application-generated; it is not sourced from Excel.
                is_bills_table = table_name.lower().replace("dbo.", "").strip() in ("bill", "bills")
                billno_source_col = next(
                    (ex for ex, db_col in excel_to_db.items()
                     if normalize_string(db_col).replace(" ", "").replace("_", "") == "billno"),
                    None,
                )
                bill_number_col_name = next(
                    (c.name for c in t_meta.columns
                     if normalize_string(c.name).replace(" ", "").replace("_", "") == "billnumber"),
                    None,
                )
                if is_bills_table and billno_source_col and bill_number_col_name:
                    included_cols.add(bill_number_col_name)
                for c in t_meta.columns:
                    if getattr(c, "identity", False):
                        included_cols.add(c.name)
                    if (
                        any(fk.constrained_column == c.name for fk in t_meta.foreign_keys)
                        or bool(getattr(c, "foreign_keys", []))
                        or is_inferred_foreign_key(c.name, active_target_tables)
                    ):
                        included_cols.add(c.name)
                    if not c.nullable and not getattr(c, "identity", False) and c.default is None:
                        included_cols.add(c.name)

                for col_name in sorted(included_cols):
                    col_meta = col_meta_map.get(col_name)
                    if not col_meta:
                        continue

                    src_col = next((ex for ex, db in excel_to_db.items() if db == col_name), None)
                    raw_val = row.get(src_col) if src_col else None
                    str_src_val = str(raw_val).strip() if (raw_val is not None and not is_empty_or_na(raw_val)) else None

                    is_ident = getattr(col_meta, "identity", False)
                    has_def = col_meta.default is not None
                    is_req = (not col_meta.nullable) and (not is_ident) and (not has_def)
                    is_oli_price = bool(find_item_price_column([col_name])) and (item_rel is not None) and getattr(settings, "ENABLE_MASTER_PRICE_MAPPING", True)

                    final_val: Optional[str] = None
                    val_src = "EXCEL"
                    val_status = "VALID"
                    val_msg: Optional[str] = None

                    # Check Foreign Key relationship
                    rel = next((r for r in lookup_relationships if r.child_table == table_name and r.child_column == col_name), None)
                    is_fk = bool(
                        rel
                        or any(fk.constrained_column == col_name for fk in t_meta.foreign_keys)
                        or is_inferred_foreign_key(col_name, active_target_tables)
                    )

                    # Bill_Number is generated by the application and explicitly sent in INSERT.
                    if is_bills_table and bill_number_col_name and col_name == bill_number_col_name:
                        final_val = "Generated by application on INSERT"
                        val_src = "APPLICATION_GENERATED"
                        val_status = "VALID"
                        val_msg = "A unique sequential value is generated by the migration engine and included in INSERT."
                    # Check Identity column
                    elif is_ident:
                        final_val = "Generated by SQL Server"
                        val_src = "IDENTITY"

                    elif is_fk:
                        val_src = "FOREIGN_KEY"
                        src_lookup_val = None
                        if rel and rel.excel_name_column and rel.excel_name_column in df.columns:
                            raw_name_val = row.get(rel.excel_name_column)
                            if raw_name_val is not None and not is_empty_or_na(raw_name_val):
                                src_lookup_val = str(raw_name_val).strip()
                        elif str_src_val is not None:
                            src_lookup_val = str_src_val

                        user_override_val = None
                        if user_value_overrides:
                            user_override_val = (
                                user_value_overrides.get(f"{table_name}.{col_name}", {}).get(src_lookup_val or "")
                                or user_value_overrides.get(col_name, {}).get(src_lookup_val or "")
                            )

                        parent_t = rel.parent_table if rel else (
                            next((fk.target_table for fk in t_meta.foreign_keys if fk.constrained_column == col_name), None)
                            or (col_name[:-3] if col_name.lower().endswith("_id") else col_name)
                        )
                        parent_id_c = rel.parent_id_column if rel else (
                            next((fk.target_column for fk in t_meta.foreign_keys if fk.constrained_column == col_name), None)
                            or col_name
                        )
                        parent_name_c = rel.parent_name_column if rel else None
                        db_recs = parent_records_by_table.get(parent_t, [])

                        res_rec = DynamicLookupResolver.resolve_foreign_key_record(
                            child_table=table_name,
                            child_column=col_name,
                            parent_table=parent_t,
                            parent_id_column=parent_id_c,
                            parent_name_column=parent_name_c,
                            db_records=db_recs,
                            excel_source_value=src_lookup_val,
                            row_resolved_entities=row_resolved_entities,
                            row_value_mappings=this_row_vms,
                            row_data=row.to_dict(),
                            user_override_id=user_override_val,
                            migration_cache=fk_migration_cache,
                            all_parent_records=parent_records_by_table
                        )

                        if res_rec.status == "RESOLVED":
                            final_val = str(res_rec.matched_primary_key_value)
                            val_src = "DATABASE_LOOKUP"
                            val_status = "VALID"
                            val_msg = None
                        elif res_rec.status == "AMBIGUOUS":
                            final_val = None
                            val_src = "AMBIGUOUS"
                            val_status = "ERROR" if is_req else "WARNING"
                            val_msg = res_rec.message
                        elif (rel and rel.parent_table in active_tables_ordered) or (parent_t and parent_t in active_tables_ordered):
                            final_val = "Generated by SQL Server (pending insertion)"
                            val_src = "GENERATED_ID"
                            val_status = "VALID"
                        elif has_def:
                            final_val = col_meta.default or "Database Default"
                            val_src = "DATABASE_DEFAULT"
                            val_status = "VALID"
                        elif col_meta.nullable:
                            final_val = "NULL"
                            val_src = "NULL"
                            val_status = "VALID"
                        else:
                            final_val = None
                            val_src = "UNRESOLVED"
                            val_status = "ERROR"
                            val_msg = res_rec.message or f"Required foreign key '{col_name}' could not be resolved."

                    # Master price resolution for order line items pointing to ItemsMaster
                    elif is_oli_price:
                        item_ent = row_resolved_entities.get(item_rel.parent_table) or row_resolved_entities.get(item_rel.parent_table.lower())
                        if not item_ent:
                            raw_name_val = row.get(item_rel.excel_name_column) if (item_rel.excel_name_column and item_rel.excel_name_column in df.columns) else None
                            s_name = str(raw_name_val).strip() if (raw_name_val is not None and not is_empty_or_na(raw_name_val)) else None
                            if s_name is None:
                                for k, v in row.to_dict().items():
                                    if normalize_string(str(k)).replace(" ", "").replace("_", "") in ("description", "item", "itemname", "item_name", "product", "product_name") and not is_empty_or_na(v):
                                        s_name = str(v).strip()
                                        break
                            item_ent = DynamicLookupResolver.resolve_foreign_key_record(
                                child_table=table_name,
                                child_column=item_rel.child_column,
                                parent_table=item_rel.parent_table,
                                parent_id_column=item_rel.parent_id_column,
                                parent_name_column=item_rel.parent_name_column,
                                db_records=parent_records_by_table.get(item_rel.parent_table, []),
                                excel_source_value=s_name,
                                row_data=row.to_dict(),
                                migration_cache=fk_migration_cache
                            )
                        if item_ent and item_ent.status == "RESOLVED":
                            master_p = extract_master_item_price(item_ent.record_data)
                            if master_p is not None:
                                final_val = f"{master_p:.2f}"
                                val_src = "DATABASE_LOOKUP"
                                val_status = "VALID"
                                val_msg = None
                            elif str_src_val is not None:
                                final_val = str_src_val
                                val_src = "EXCEL"
                        elif item_ent and item_ent.status == "AMBIGUOUS":
                            val_status = "WARNING"
                            val_msg = f"Item is ambiguous in {item_rel.parent_table}; master price unresolved."
                            if str_src_val is not None:
                                final_val = str_src_val
                                val_src = "EXCEL"
                        elif item_ent and item_ent.status != "RESOLVED":
                            val_status = "WARNING" if col_meta.nullable else "ERROR"
                            val_msg = f"Item unresolved in {item_rel.parent_table}; master price unresolved."
                            if str_src_val is not None:
                                final_val = str_src_val
                                val_src = "EXCEL"

                    # Regular column
                    elif str_src_val is not None:
                        mapped_val = value_transform_lookup.get((table_name, col_name, str_src_val)) or col_value_transform.get((col_name, str_src_val))
                        if mapped_val is not None:
                            final_val = mapped_val
                            val_src = "VALUE_MAPPING" if mapped_val != str_src_val else "EXCEL"
                        else:
                            # If target column is date/time, format using shared normalize_date_value
                            col_t_up = col_meta.type.upper() if col_meta else ""
                            if any(d in col_t_up for d in ("DATE", "DATETIME", "DATETIME2", "SMALLDATETIME")):
                                from app.utils.normalization import normalize_date_value
                                norm_d, _, is_vd = normalize_date_value(raw_val, target_type=col_t_up)
                                final_val = norm_d if (is_vd and norm_d is not None) else str_src_val
                            else:
                                final_val = str_src_val
                            val_src = "EXCEL"
                    else:
                        t_fk_cols = {
                            getattr(fk, "constrained_column", None) for fk in getattr(t_meta, "foreign_keys", [])
                        } | {
                            r.child_column for r in lookup_relationships if r.child_table == table_name
                        }
                        raw_str = str(raw_val).strip() if raw_val is not None else ""
                        mapped_val = (
                            value_transform_lookup.get((table_name, col_name, raw_str))
                            or col_value_transform.get((col_name, raw_str))
                        ) if raw_str else None

                        if mapped_val == "null":
                            # Legitimate business value 'null' from explicit value mapping / override
                            final_val = "null"
                            val_src = "VALUE_MAPPING"
                            val_status = "VALID"
                            val_msg = None
                        elif should_column_receive_empty_string(col_name, table_name, col_meta, t_meta, t_fk_cols):
                            # String/text columns expecting an empty string normalize None, 'null', 'NULL', '' to ''
                            final_val = ""
                            val_src = "EXCEL" if src_col else "EMPTY_STRING"
                            val_status = "VALID"
                            val_msg = None
                        else:
                            t_ent_idx = entity_indices.get(table_name)
                            m_res = MissingValueResolver.resolve_missing_value(
                                table_name=table_name,
                                target_column=col_name,
                                table_meta=t_meta,
                                source_row_data=row.to_dict(),
                                db_to_excel={m.database_column: m.excel_column for m in mappings if m.excel_column in df.columns},
                                entity_lookup_index=t_ent_idx
                            )
                            if m_res.status == "RESOLVED":
                                final_val = str(m_res.value)
                                val_src = m_res.value_source
                                val_status = "VALID"
                                val_msg = m_res.message
                            elif m_res.status == "DEFAULT":
                                final_val = str(m_res.value) if m_res.value is not None else (col_meta.default or "Database Default")
                                val_src = "DATABASE_DEFAULT"
                                val_status = "VALID"
                                val_msg = m_res.message
                            elif m_res.status == "NULLABLE":
                                final_val = "NULL"
                                val_src = "NULL"
                                val_status = "VALID"
                            else:
                                final_val = None
                                val_src = "UNRESOLVED"
                                val_status = "ERROR" if is_req else "WARNING"
                                val_msg = m_res.message

                    m_info = mapping_dict.get(col_name)
                    m_status = m_info.status if m_info else ("SYSTEM" if is_ident else "UNMAPPED")
                    m_conf = m_info.confidence if m_info else 1.0

                    if is_ident:
                        req_type = "AUTO_GENERATED"
                    elif has_def:
                        req_type = "DATABASE_DEFAULT"
                    elif val_src in ("UNRESOLVED", "AMBIGUOUS") or (is_req and (final_val is None or val_status == "ERROR")):
                        req_type = "UNRESOLVED"
                    elif is_fk:
                        req_type = "FOREIGN_KEY"
                    elif is_req:
                        req_type = "MANDATORY"
                    else:
                        req_type = "OPTIONAL"

                    preview_rows.append(SourceRowPreviewItem(
                        sourceRow=source_row_num,
                        sourceFile=file_name,
                        targetDatabase=db_name,
                        targetSchema=getattr(t_meta, "schema_name", "dbo"),
                        targetTable=table_name,
                        targetColumn=col_name,
                        sourceColumn=src_col,
                        sourceValue=str_src_val,
                        finalValue=final_val,
                        valueSource=val_src,
                        mappingStatus=m_status,
                        mappingConfidence=m_conf,
                        isNullable=col_meta.nullable,
                        isRequired=is_req,
                        isIdentity=is_ident,
                        hasDefault=has_def,
                        requirementType=req_type,
                        validationStatus=val_status,
                        validationMessage=val_msg
                    ))

        # 9. Planned Operations Summary
        total_planned_ins = sum(planned_inserts_by_table.values())
        total_planned_skips = sum(planned_skips_by_table.values())

        planned_ops = PlannedOperationsSummary(
            totalPlannedInserts=total_planned_ins,
            totalPlannedUpdates=0,
            totalPlannedSkips=total_planned_skips,
            plannedInsertsByTable=planned_inserts_by_table,
            plannedUpdatesByTable={},
            plannedSkipsByTable=planned_skips_by_table,
            tableRoles=table_roles,
            skippedTables=skipped_tables_dict,
            targetInsertTables=target_insert_tables_list,
            lookupOnlyTables=lookup_only_tables_list,
            dependencyInsertTables=dependency_insert_tables_list,
            statement="This preview has not written any data to the database.",
            canExecute=validation_summary.can_execute
        )

        t_end = time.time()
        logger.info(f"[STAGE] SQL_PREVIEW_COMPLETED | rows={len(preview_rows)} | {t_end - t_prev_start:.3f}s")
        logger.info(f"[STAGE] DRY_RUN_COMPLETED | total_time={t_end - t0:.3f}s | zero writes performed")

        return PreviewResultTuple(
            preview_rows=preview_rows,
            dependency_plan=dependency_plan,
            planned_ops=planned_ops,
            validation_summary=validation_summary,
            active_tables_ordered=active_tables_ordered,
            value_resolutions=value_resolutions,
            value_resolution_summary=value_resolution_summary,
            value_mappings=all_value_mappings,
            item_price_resolutions=item_price_resolutions
        )


import re
import math
import datetime
from typing import List, Dict, Any, Optional, Set, Tuple
import pandas as pd
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.models.migration import (
    ColumnMappingResult,
    RowValidationError,
    ValidationSummary,
    ValueMappingItem,
    ForeignKeyPlanItem
)
from app.migration.dependency import DependencyResolver
from app.migration.missing_value_resolver import MissingValueResolver, TableEntityLookupIndex
from app.mapping.matcher import resolve_unmapped_required_columns, normalize_string
from app.core.logging import logger


def is_empty_or_na(val: Any) -> bool:
    """Checks if a cell value is null, NaN, NaT, or empty/whitespace."""
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return True
    if pd.isna(val):
        return True
    s = str(val).strip()
    return s == "" or s.lower() in ("nan", "none", "null", "nat")


def is_inferred_foreign_key(col_name: str, target_tables: List[str]) -> bool:
    """Detects if a column is a logical foreign key referencing a parent table."""
    c_lower = col_name.lower()
    if c_lower.endswith("_id") or c_lower.endswith("id"):
        for t in target_tables:
            t_lower = t.lower()
            if t_lower in c_lower:
                return True
        if any(k in c_lower for k in ("outlet", "customer", "order", "bill", "item")):
            return True
    return False


class MigrationValidator:
    """
    High-Performance Vectorized Dataset Validator.
    Validates the ENTIRE dataset across all source rows against the target SQL Server schema in milliseconds.
    Validates:
    - Required columns and missing values (with automatic database lookup & default resolution)
    - SQL Server data types and lengths
    - Primary keys and unique constraints
    - Foreign-key existence and ambiguity
    - Identity columns and database defaults
    - Duplicate source records
    - Scoped dependency ordering and circular foreign-key relationships
    """

    @staticmethod
    def validate_dataset(
        df: pd.DataFrame,
        schema: DatabaseSchemaMetadata,
        column_mappings: List[ColumnMappingResult],
        existing_lookup: Optional[Dict[str, Set[str]]] = None,
        parent_pending_inserts: Optional[Dict[str, Set[str]]] = None,
        value_mappings: Optional[List[ValueMappingItem]] = None,
        foreign_key_plans: Optional[List[ForeignKeyPlanItem]] = None,
        entity_indices: Optional[Dict[str, TableEntityLookupIndex]] = None,
        approved_rules: Optional[Dict[str, Any]] = None,
        lookup_relationships: Optional[List[Any]] = None,
        parent_records_by_table: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        upload_id: Optional[str] = None
    ) -> ValidationSummary:
        errors: List[RowValidationError] = []
        warnings: List[RowValidationError] = []
        invalid_row_set: Set[int] = set()

        total_rows = len(df)
        existing_lookup = existing_lookup or {}
        parent_pending_inserts = parent_pending_inserts or {}

        schema_tables = {t.name: t for t in schema.tables}

        # Filter matched mappings targeting existing schema tables (ignore DO_NOT_MIGRATE and UNKNOWN)
        matched_mappings = [
            m for m in column_mappings
            if m.status == "MATCHED" and m.database_table in schema_tables
        ]
        matched_cols_count = len(matched_mappings)
        unmatched_cols_count = len([m for m in column_mappings if m.status != "MATCHED"])

        # Group mappings by target table
        table_mappings: Dict[str, List[ColumnMappingResult]] = {}
        for m in matched_mappings:
            if m.database_table.lower() in ("outlets", "dbo.outlets"):
                continue
            table_mappings.setdefault(m.database_table, []).append(m)

        # Dynamically resolve unmapped required columns from source dataset
        table_mappings = resolve_unmapped_required_columns(schema, table_mappings, df.columns.tolist())

        # Discover lookup relationships if not provided
        if lookup_relationships is None:
            from app.migration.lookup_resolver import DynamicLookupResolver
            lookup_relationships = DynamicLookupResolver.discover_relationships(
                schema=schema,
                df_columns=df.columns.tolist(),
                column_mappings=column_mappings,
                active_tables=list(table_mappings.keys())
            )

        READ_ONLY_REFERENCE_TABLES = {"outlets", "dbo.outlets", "itemsmaster", "dbo.itemsmaster"}
        active_target_tables = [
            t for t in table_mappings
            if t.lower() not in READ_ONLY_REFERENCE_TABLES
        ]

        # 1. Scoped Dependency & Cycle Detection (strictly scoped to active target tables)
        _, cycles = DependencyResolver.build_dependency_graph(schema, target_tables=active_target_tables)
        for c in cycles:
            warnings.append(RowValidationError(
                rowNumber=0,
                tableName="SCHEMA",
                columnName="DEPENDENCY",
                errorType="CIRCULAR_DEPENDENCY",
                message=c,
                severity="WARNING",
                recommendedCorrection="Review dependency graph or verify parent-child table insertion hierarchy."
            ))

        unresolved_fks_count = 0

        # 2. Table-level duplicate mapping, required column, and foreign-key check
        for table_name, mappings in table_mappings.items():
            t_meta = schema_tables[table_name]
            col_meta_map = {c.name: c for c in t_meta.columns}

            # Check for duplicate target-column mappings on the same table
            db_col_counts: Dict[str, List[str]] = {}
            for m in mappings:
                if m.excel_column in df.columns:
                    db_col_counts.setdefault(m.database_column, []).append(m.excel_column)

            for target_col, src_cols in db_col_counts.items():
                if len(src_cols) > 1:
                    # If the table is Outlets (which is a read-only reference table evaluated via outlet matcher),
                    # multiple outlet sources (restaurant_name, virtual_brand_name) are evaluated per row rather than inserted.
                    if table_name.lower() in ("outlets", "dbo.outlets") and target_col.lower() in ("outlet_name", "outletname"):
                        continue
                    errors.append(RowValidationError(
                        rowNumber=0,
                        tableName=table_name,
                        columnName=target_col,
                        sourceColumn=", ".join(src_cols),
                        invalidValue=None,
                        errorType="DUPLICATE_TARGET_COLUMN",
                        message=(
                            f"Duplicate target column mapping detected on table '{table_name}': "
                            f"Multiple source columns {src_cols} map to target column '{target_col}'. "
                            f"Each target column may only appear once in an INSERT statement."
                        ),
                        recommendedCorrection=(
                            f"Disambiguate column mappings or configure business rules so only one source maps to '{table_name}.{target_col}'."
                        ),
                        severity="ERROR"
                    ))

            excel_to_db = {m.excel_column: m.database_column for m in mappings if m.excel_column in df.columns}
            db_to_excel = {m.database_column: m.excel_column for m in mappings if m.excel_column in df.columns}
            table_rels = {r.child_column: r for r in (lookup_relationships or []) if r.child_table == table_name}

            for c_name, c_meta in col_meta_map.items():
                is_identity = getattr(c_meta, "identity", False)
                has_default = c_meta.default is not None
                is_nullable = c_meta.nullable

                # Required = NOT nullable AND NOT identity AND has no database default
                is_required = (not is_nullable) and (not is_identity) and (not has_default)

                rel = table_rels.get(c_name)
                if is_required and rel and rel.parent_table.lower() in ("outlets", "dbo.outlets"):
                    from app.migration.lookup_resolver import DynamicLookupResolver
                    from app.migration.outlet_matcher import outlet_row_source_values

                    parent_records = (parent_records_by_table or {}).get(rel.parent_table, [])
                    valid_ids = {
                        str(record.get(rel.parent_id_column))
                        for record in parent_records
                        if record.get(rel.parent_id_column) is not None
                    }
                    direct_source_col = db_to_excel.get(c_name)
                    for b_idx in df.index:
                        row_data = df.loc[b_idx].to_dict()
                        src_val = row_data.get(rel.excel_name_column) if rel.excel_name_column else None
                        if src_val is None and direct_source_col:
                            src_val = row_data.get(direct_source_col)
                        resolved = DynamicLookupResolver.resolve_foreign_key_record(
                            child_table=table_name,
                            child_column=c_name,
                            parent_table=rel.parent_table,
                            parent_id_column=rel.parent_id_column,
                            parent_name_column=rel.parent_name_column,
                            db_records=parent_records,
                            excel_source_value=src_val,
                            row_data=row_data,
                            upload_id=upload_id,
                            source_row_num=int(b_idx) + 1
                        )
                        restaurant_value, virtual_brand_value = outlet_row_source_values(row_data)
                        direct_value = row_data.get(direct_source_col) if direct_source_col else None
                        direct_id = None
                        direct_id_reason = None
                        if not is_empty_or_na(direct_value):
                            try:
                                numeric_value = float(str(direct_value).strip())
                                if not numeric_value.is_integer():
                                    raise ValueError
                                direct_id = int(numeric_value)
                            except (TypeError, ValueError):
                                direct_id_reason = f"Mapped {c_name} value {direct_value!r} is not an integer ID."

                        resolved_id = resolved.matched_primary_key_value
                        names_present = bool(restaurant_value or virtual_brand_value or (rel.excel_name_column and row_data.get(rel.excel_name_column)) or (resolved.status == "RESOLVED" and str(resolved_id) in valid_ids))
                        if names_present:
                            valid_resolution = (
                                resolved.status == "RESOLVED"
                                and isinstance(resolved_id, int)
                                and not isinstance(resolved_id, bool)
                                and str(resolved_id) in valid_ids
                            )
                            if valid_resolution and direct_id is not None and direct_id != resolved_id:
                                direct_id_reason = (
                                    f"Mapped {c_name} ID {direct_id} conflicts with name-resolved outlet ID {resolved_id}."
                                )
                            elif valid_resolution:
                                direct_id_reason = None
                        else:
                            valid_resolution = (
                                direct_id is not None
                                and str(direct_id) in valid_ids
                            )
                            if valid_resolution:
                                resolved_id = direct_id

                        if valid_resolution and not direct_id_reason:
                            continue

                        row_num = int(b_idx) + 1
                        invalid_row_set.add(row_num)
                        reason = direct_id_reason or resolved.message
                        if not reason:
                            reason = "No unique existing outlet ID was resolved."
                        error_type = (
                            resolved.status
                            if resolved.status in ("AMBIGUOUS", "CONFLICTING_OUTLET_VALUES")
                            else "UNRESOLVED_REQUIRED_FOREIGN_KEY"
                        )
                        errors.append(RowValidationError(
                            rowNumber=row_num,
                            tableName=table_name,
                            columnName=c_name,
                            sourceColumn=direct_source_col or rel.excel_name_column,
                            invalidValue=f"{restaurant_value or ''} | {virtual_brand_value or ''}"[:100],
                            errorType=error_type,
                            message=(
                                f"Row {row_num}: Required foreign key '{c_name}' in '{table_name}' "
                                f"referencing '{rel.parent_table}.{rel.parent_id_column}' was not resolved: {reason}"
                            ),
                            recommendedCorrection="Provide a unique existing outlet name or a valid existing Outlet_ID.",
                            severity="ERROR"
                        ))
                    continue

                if is_required and c_name not in db_to_excel:
                    rel = table_rels.get(c_name)
                    if rel:
                        parent_tbl = rel.parent_table
                        parent_id_col = rel.parent_id_column
                        src_col = rel.excel_name_column or rel.excel_id_column

                        if parent_tbl.lower() in ("outlets", "dbo.outlets"):
                            from app.migration.lookup_resolver import DynamicLookupResolver
                            from app.migration.outlet_matcher import outlet_row_source_values

                            parent_records = (parent_records_by_table or {}).get(parent_tbl, [])
                            for b_idx in df.index:
                                row_data = df.loc[b_idx].to_dict()
                                resolved = DynamicLookupResolver.resolve_foreign_key_record(
                                    child_table=table_name,
                                    child_column=c_name,
                                    parent_table=parent_tbl,
                                    parent_id_column=parent_id_col,
                                    parent_name_column=rel.parent_name_column,
                                    db_records=parent_records,
                                    row_data=row_data,
                                    upload_id=upload_id,
                                    source_row_num=int(b_idx) + 1
                                )
                                valid_ids = {
                                    str(record.get(parent_id_col))
                                    for record in parent_records
                                    if record.get(parent_id_col) is not None
                                }
                                resolved_id = resolved.matched_primary_key_value
                                if (
                                    resolved.status == "RESOLVED"
                                    and isinstance(resolved_id, int)
                                    and not isinstance(resolved_id, bool)
                                    and str(resolved_id) in valid_ids
                                ):
                                    continue

                                row_num = int(b_idx) + 1
                                restaurant_value, virtual_brand_value = outlet_row_source_values(row_data)
                                invalid_row_set.add(row_num)
                                error_type = (
                                    resolved.status
                                    if resolved.status in ("AMBIGUOUS", "CONFLICTING_OUTLET_VALUES")
                                    else "UNRESOLVED_REQUIRED_FOREIGN_KEY"
                                )
                                errors.append(RowValidationError(
                                    rowNumber=row_num,
                                    tableName=table_name,
                                    columnName=c_name,
                                    sourceColumn=src_col,
                                    invalidValue=f"{restaurant_value or ''} | {virtual_brand_value or ''}"[:100],
                                    errorType=error_type,
                                    message=(
                                        f"Row {row_num}: Required foreign key '{c_name}' in '{table_name}' "
                                        f"referencing '{parent_tbl}.{parent_id_col}' was not resolved: {resolved.message}"
                                    ),
                                    recommendedCorrection="Provide a unique value identifying an existing outlet in dbo.Outlets.",
                                    severity="ERROR"
                                ))
                            continue

                        has_vm_parent = any(
                            (vm.matched_table and vm.matched_table.lower() == parent_tbl.lower())
                            or (vm.matched_record_data and (parent_id_col in vm.matched_record_data or "Outlet_ID" in vm.matched_record_data))
                            for vm in (value_mappings or [])
                        )

                        if not src_col or src_col not in df.columns:
                            if not has_vm_parent and parent_tbl not in active_target_tables:
                                msg = (
                                    f"Required foreign key column '{c_name}' in target table '{table_name}' "
                                    f"(referencing '{parent_tbl}.{parent_id_col}') cannot be resolved because no matching column was found in the source file."
                                )
                                errors.append(RowValidationError(
                                    rowNumber=0,
                                    tableName=table_name,
                                    columnName=c_name,
                                    sourceColumn=None,
                                    invalidValue=None,
                                    errorType="REQUIRED_FOREIGN_KEY_UNMAPPED",
                                    message=msg,
                                    recommendedCorrection=f"Provide an identifying column in Excel (e.g. '{rel.parent_name_column or parent_tbl + ' name'}') or map '{c_name}'.",
                                    severity="ERROR"
                                ))
                        else:
                            series_raw = df[src_col]
                            series_str = series_raw.fillna("").astype(str).str.strip()
                            null_mask = series_raw.isna() | series_str.isin(["", "nan", "none", "null", "nat"])

                            if null_mask.any() and not has_vm_parent:
                                bad_indices = df.index[null_mask].tolist()
                                for b_idx in bad_indices[:50]:
                                    row_num = int(b_idx) + 1
                                    invalid_row_set.add(row_num)
                                    errors.append(RowValidationError(
                                        rowNumber=row_num,
                                        tableName=table_name,
                                        columnName=c_name,
                                        sourceColumn=src_col,
                                        invalidValue=None,
                                        errorType="MISSING_REQUIRED_FOREIGN_KEY",
                                        message=f"Row {row_num}: Required foreign key '{c_name}' in '{table_name}' referencing '{parent_tbl}' is missing identifying value in '{src_col}'.",
                                        recommendedCorrection=f"Provide an identifying {parent_tbl} name or code in row {row_num}.",
                                        severity="ERROR"
                                    ))

                            if parent_tbl not in active_target_tables:
                                non_null_mask = ~null_mask
                                if non_null_mask.any():
                                    from app.migration.lookup_resolver import DynamicLookupResolver
                                    p_recs = (parent_records_by_table or {}).get(parent_tbl, [])
                                    p_idx = DynamicLookupResolver.get_or_create_index(
                                        db_records=p_recs,
                                        parent_id_column=parent_id_col,
                                        parent_name_column=rel.parent_name_column or parent_id_col
                                    )
                                    unique_vals = series_str[non_null_mask].unique()
                                    unresolved_val_set = set()
                                    for u_val in unique_vals:
                                        if (
                                            u_val in p_idx.exact_map
                                            or normalize_string(u_val) in p_idx.norm_map
                                            or u_val in p_idx.override_map
                                        ):
                                            continue
                                        res_rec = DynamicLookupResolver.resolve_foreign_key_record(
                                            child_table=table_name,
                                            child_column=c_name,
                                            parent_table=parent_tbl,
                                            parent_id_column=parent_id_col,
                                            parent_name_column=rel.parent_name_column or parent_id_col,
                                            db_records=p_recs,
                                            excel_source_value=u_val,
                                            index=p_idx,
                                            all_parent_records=parent_records_by_table or {}
                                        )
                                        if res_rec.status != "RESOLVED":
                                            unresolved_val_set.add(u_val)

                                    if unresolved_val_set:
                                        bad_indices = df.index[non_null_mask & series_str.isin(unresolved_val_set)].tolist()
                                        for b_idx in bad_indices[:50]:
                                            row_num = int(b_idx) + 1
                                            raw_v = str(df.at[b_idx, src_col])
                                            invalid_row_set.add(row_num)
                                            errors.append(RowValidationError(
                                                rowNumber=row_num,
                                                tableName=table_name,
                                                columnName=c_name,
                                                sourceColumn=src_col,
                                                invalidValue=raw_v[:50],
                                                errorType="UNRESOLVED_REQUIRED_FOREIGN_KEY",
                                                message=f"Row {row_num}: Required foreign key '{c_name}' in '{table_name}' referencing '{parent_tbl}' could not be resolved: '{raw_v}' not found in database.",
                                                recommendedCorrection=f"Ensure parent record '{raw_v}' exists in database table '{parent_tbl}' or configure a synonym.",
                                                severity="ERROR"
                                            ))
                    else:
                        is_explicit_fk = any(fk.constrained_column == c_name for fk in t_meta.foreign_keys)
                        is_inferred_fk = is_inferred_foreign_key(c_name, active_target_tables)
                        if not (is_explicit_fk or is_inferred_fk):
                            msg = (
                                f"Required column '{c_name}' in target table '{table_name}' "
                                f"(NOT NULL without default) is not mapped from source file."
                            )
                            errors.append(RowValidationError(
                                rowNumber=0,
                                tableName=table_name,
                                columnName=c_name,
                                sourceColumn=None,
                                invalidValue=None,
                                errorType="REQUIRED_COLUMN_UNMAPPED",
                                message=msg,
                                recommendedCorrection=f"Map a source column to '{table_name}.{c_name}' or configure a default value.",
                                severity="ERROR"
                            ))
                        else:
                            msg = (
                                f"Required foreign key column '{c_name}' in target table '{table_name}' "
                                f"cannot be resolved because no foreign-key relationship or source column could be matched."
                            )
                            errors.append(RowValidationError(
                                rowNumber=0,
                                tableName=table_name,
                                columnName=c_name,
                                sourceColumn=None,
                                invalidValue=None,
                                errorType="REQUIRED_FOREIGN_KEY_UNMAPPED",
                                message=msg,
                                recommendedCorrection=f"Map a source column to '{table_name}.{c_name}' or provide an identifying parent column.",
                                severity="ERROR"
                            ))

            # 3. Fast Vectorized Column Validation across all rows
            # Determine which rows actually contain data for this entity/table
            table_has_data_mask = pd.Series(False, index=df.index)
            for ex in excel_to_db.keys():
                s = df[ex].fillna("").astype(str).str.strip()
                table_has_data_mask |= (~df[ex].isna() & ~s.isin(["", "nan", "none", "null", "nat"]))

            for ex_col, db_col in excel_to_db.items():
                if db_col not in col_meta_map or ex_col not in df.columns:
                    continue

                c_meta = col_meta_map[db_col]
                is_identity = getattr(c_meta, "identity", False)
                has_default = c_meta.default is not None
                is_required = (not c_meta.nullable) and (not is_identity) and (not has_default)

                # Vectorized null detection
                series_raw = df[ex_col]
                series_str = series_raw.fillna("").astype(str).str.strip()
                null_mask = series_raw.isna() | series_str.isin(["", "nan", "none", "null", "nat"])
                non_null_mask = ~null_mask

                is_fk = (
                    any(fk.constrained_column == db_col for fk in t_meta.foreign_keys)
                    or bool(getattr(c_meta, "foreign_keys", []))
                    or is_inferred_foreign_key(db_col, active_target_tables)
                )

                fk_resolved_or_planned = False
                if is_fk and foreign_key_plans:
                    fk_resolved_or_planned = any(
                        fk_p.child_table == table_name and fk_p.child_column == db_col and fk_p.status in ("MATCHED", "PENDING", "RESOLVED", "RESOLVED_FROM_DB")
                        for fk_p in foreign_key_plans
                    )

                # Check missing required values (with automatic database lookup & default resolution)
                if is_required and not fk_resolved_or_planned and null_mask.any():
                    bad_indices = df.index[null_mask].tolist()
                    t_entity_idx = (entity_indices or {}).get(table_name)

                    unresolved_entries = []
                    for b_idx in bad_indices:
                        row_data = df.iloc[b_idx].to_dict()
                        res = MissingValueResolver.resolve_missing_value(
                            table_name=table_name,
                            target_column=db_col,
                            table_meta=t_meta,
                            source_row_data=row_data,
                            db_to_excel=db_to_excel,
                            entity_lookup_index=t_entity_idx,
                            approved_business_rules=approved_rules
                        )
                        if res.status in ("RESOLVED", "DEFAULT", "NULLABLE"):
                            # Dynamically resolved from existing database records or default constraint!
                            continue
                        elif res.status == "AMBIGUOUS":
                            row_num = int(b_idx) + 1
                            invalid_row_set.add(row_num)
                            errors.append(RowValidationError(
                                rowNumber=row_num,
                                tableName=table_name,
                                columnName=db_col,
                                sourceColumn=ex_col,
                                invalidValue=None,
                                errorType="AMBIGUOUS_RECORD_MATCH",
                                message=f"Row {row_num}: {res.message}",
                                recommendedCorrection="Review multiple matching records in database or provide explicit unique key.",
                                severity="WARNING"
                            ))
                            continue
                        else:
                            unresolved_entries.append((b_idx, res.message))

                    for b_idx, reason_msg in unresolved_entries[:50]:  # Capture sample unresolved rows
                        row_num = int(b_idx) + 1
                        invalid_row_set.add(row_num)
                        errors.append(RowValidationError(
                            rowNumber=row_num,
                            tableName=table_name,
                            columnName=db_col,
                            sourceColumn=ex_col,
                            invalidValue=None,
                            errorType="MISSING_REQUIRED_VALUE",
                            message=f"Row {row_num}: Required column '{db_col}' in '{table_name}' has missing/NULL value. {reason_msg}",
                            recommendedCorrection="Ensure an existing database record matches the unique lookup key or configure a database default constraint.",
                            severity="ERROR"
                        ))



                # If non-null values exist, validate length and type
                if non_null_mask.any():
                    valid_str_series = series_str[non_null_mask]

                    # Max Length Check (applies strictly to text/character columns, not binary/datetime/numeric bytes)
                    col_type_upper = c_meta.type.upper()
                    is_string_col = any(t in col_type_upper for t in ("CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT"))

                    if is_string_col and c_meta.max_length and c_meta.max_length > 0:
                        effective_len = c_meta.max_length
                        if "NCHAR" in col_type_upper or "NVARCHAR" in col_type_upper:
                            effective_len = c_meta.max_length // 2

                        if effective_len > 0:
                            too_long_mask = valid_str_series.str.len() > effective_len

                            if too_long_mask.any():
                                bad_indices = valid_str_series[too_long_mask].index.tolist()
                                for b_idx in bad_indices[:50]:
                                    row_num = int(b_idx) + 1
                                    raw_val = str(df.at[b_idx, ex_col])
                                    invalid_row_set.add(row_num)
                                    errors.append(RowValidationError(
                                        rowNumber=row_num,
                                        tableName=table_name,
                                        columnName=db_col,
                                        sourceColumn=ex_col,
                                        invalidValue=raw_val[:50],
                                        errorType="MAX_LENGTH_EXCEEDED",
                                        message=f"Row {row_num}: Column '{db_col}' value length ({len(raw_val)}) exceeds limit ({effective_len}).",
                                        recommendedCorrection=f"Shorten value to at most {effective_len} characters.",
                                        severity="ERROR"
                                    ))

                    # Type Conversion Check
                    col_type_upper = c_meta.type.upper()

                    # Numeric / Float / Money / Decimal / Integer
                    if not fk_resolved_or_planned and any(t in col_type_upper for t in ("INT", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DECIMAL", "NUMERIC", "MONEY", "REAL", "DOUBLE")):
                        clean_num_series = valid_str_series.str.replace("$", "", regex=False).str.replace("₹", "", regex=False).str.replace(",", "", regex=False)
                        coerced_numeric = pd.to_numeric(clean_num_series, errors='coerce')
                        bad_num_mask = coerced_numeric.isna()
                        if bad_num_mask.any():
                            bad_indices = clean_num_series[bad_num_mask].index.tolist()
                            for b_idx in bad_indices[:50]:
                                row_num = int(b_idx) + 1
                                raw_val = str(df.at[b_idx, ex_col])
                                invalid_row_set.add(row_num)
                                errors.append(RowValidationError(
                                    rowNumber=row_num,
                                    tableName=table_name,
                                    columnName=db_col,
                                    sourceColumn=ex_col,
                                    invalidValue=raw_val[:50],
                                    errorType="DATA_TYPE_CONVERSION",
                                    message=f"Row {row_num}: Column '{db_col}' ({c_meta.type}) cannot parse numeric value '{raw_val}'.",
                                    recommendedCorrection="Ensure numeric/decimal format without invalid characters.",
                                    severity="ERROR"
                                ))

                    # Date / Datetime
                    elif any(t in col_type_upper for t in ("DATE", "DATETIME", "DATETIME2", "SMALLDATETIME", "TIMESTAMP")):
                        from app.utils.normalization import normalize_date_value
                        unique_date_vals = valid_str_series.unique()
                        invalid_unique_dates = set()
                        for u_val in unique_date_vals:
                            _, _, is_v = normalize_date_value(u_val, target_type=col_type_upper)
                            if not is_v:
                                invalid_unique_dates.add(u_val)

                        bad_indices = valid_str_series[valid_str_series.isin(invalid_unique_dates)].index.tolist() if invalid_unique_dates else []

                        if bad_indices:
                            for b_idx in bad_indices[:50]:
                                row_num = int(b_idx) + 1
                                raw_val = str(df.at[b_idx, ex_col])
                                invalid_row_set.add(row_num)
                                errors.append(RowValidationError(
                                    rowNumber=row_num,
                                    tableName=table_name,
                                    columnName=db_col,
                                    sourceColumn=ex_col,
                                    invalidValue=raw_val[:50],
                                    errorType="DATA_TYPE_CONVERSION",
                                    message=f"Row {row_num}: Column '{db_col}' ({c_meta.type}) cannot parse date/time value '{raw_val}'.",
                                    recommendedCorrection="Ensure valid date/time format (YYYY-MM-DD, DD-MM-YYYY, or Excel date).",
                                    severity="ERROR"
                                ))

        # 4. Value Mapping Constraints Validation
        if value_mappings:
            for vm in value_mappings:
                t_meta = schema_tables.get(vm.database_table)
                c_meta = next((c for c in t_meta.columns if c.name == vm.database_column), None) if t_meta else None
                is_req = bool(c_meta and not c_meta.nullable and not getattr(c_meta, "identity", False) and c_meta.default is None)

                # Check Ambiguous Value Mappings
                if vm.status == "AMBIGUOUS":
                    msg = (
                        f"Column '{vm.database_column}' in table '{vm.database_table}' has ambiguous value '{vm.excel_value}' "
                        f"matching multiple database candidates. User review required."
                    )
                    cand_text = ", ".join([c["value"] for c in vm.candidates]) if vm.candidates else "candidates"
                    err_item = RowValidationError(
                        rowNumber=0,
                        tableName=vm.database_table,
                        columnName=vm.database_column,
                        sourceColumn=vm.excel_column,
                        invalidValue=vm.excel_value,
                        errorType="AMBIGUOUS_VALUE_MAPPING",
                        message=msg,
                        recommendedCorrection=f"Confirm candidate value ({cand_text}) in the Value Mappings tab.",
                        severity="ERROR" if is_req else "WARNING"
                    )
                    if is_req:
                        errors.append(err_item)
                    else:
                        warnings.append(err_item)

                # Check Unmatched Value Mappings on required columns
                elif vm.status == "UNMATCHED" and is_req:
                    msg = (
                        f"Required column '{vm.database_column}' in table '{vm.database_table}' contains unmatched value '{vm.excel_value}'."
                    )
                    errors.append(RowValidationError(
                        rowNumber=0,
                        tableName=vm.database_table,
                        columnName=vm.database_column,
                        sourceColumn=vm.excel_column,
                        invalidValue=vm.excel_value,
                        errorType="UNMATCHED_VALUE_MAPPING",
                        message=msg,
                        recommendedCorrection=f"Map '{vm.excel_value}' to an approved database value or configure a synonym.",
                        severity="ERROR"
                    ))

                # Check Target Value Length Constraint
                if vm.target_value and c_meta and c_meta.max_length and c_meta.max_length > 0:
                    effective_len = c_meta.max_length
                    col_type_up = c_meta.type.upper()
                    if "NCHAR" in col_type_up or "NVARCHAR" in col_type_up:
                        effective_len = c_meta.max_length // 2
                    if effective_len > 0 and len(vm.target_value) > effective_len:
                        errors.append(RowValidationError(
                            rowNumber=0,
                            tableName=vm.database_table,
                            columnName=vm.database_column,
                            sourceColumn=vm.excel_column,
                            invalidValue=vm.target_value,
                            errorType="MAX_LENGTH_EXCEEDED",
                            message=f"Mapped value '{vm.target_value}' length ({len(vm.target_value)}) exceeds column length limit ({effective_len}).",
                            recommendedCorrection=f"Shorten value to at most {effective_len} characters.",
                            severity="ERROR"
                        ))

        # 5. Foreign Key & Dynamic Name-to-ID Lookup Validation
        if foreign_key_plans:
            for fk_item in foreign_key_plans:
                c_tbl_meta = schema_tables.get(fk_item.child_table)
                c_col_meta = next((c for c in c_tbl_meta.columns if c.name == fk_item.child_column), None) if c_tbl_meta else None
                is_req = bool(c_col_meta and not c_col_meta.nullable and c_col_meta.default is None and not getattr(c_col_meta, "identity", False))

                if fk_item.status == "AMBIGUOUS":
                    unresolved_fks_count += 1
                    msg = (
                        f"Foreign key '{fk_item.child_column}' in table '{fk_item.child_table}' has ambiguous reference '{fk_item.source_business_key}' "
                        f"matching multiple database records in '{fk_item.parent_table}'. User review required."
                    )
                    cand_text = ", ".join([f"ID={c.get('id')}:{c.get('name')}" for c in getattr(fk_item, "candidates", [])]) or "candidates"
                    err = RowValidationError(
                        rowNumber=0,
                        tableName=fk_item.child_table,
                        columnName=fk_item.child_column,
                        sourceColumn=fk_item.source_business_key,
                        invalidValue=fk_item.source_business_key,
                        errorType="AMBIGUOUS_FOREIGN_KEY",
                        message=msg,
                        recommendedCorrection=f"Select the intended parent record from candidates ({cand_text}).",
                        severity="ERROR" if is_req else "WARNING"
                    )
                    if is_req:
                        errors.append(err)
                    else:
                        warnings.append(err)

                elif fk_item.status in ("NOT_FOUND", "UNRESOLVED", "NO_MATCH", "NO_RECORDS"):
                    unresolved_fks_count += 1
                    ref_key = fk_item.source_business_key
                    if ref_key is None or str(ref_key).strip() == "" or str(ref_key).strip().lower() in ("nan", "none", "null", "nat"):
                        msg = (
                            f"Required foreign key '{fk_item.child_column}' in table '{fk_item.child_table}' "
                            f"referencing '{fk_item.parent_table}' is missing identifying name in source data."
                        )
                        corr = f"Provide an identifying business name for '{fk_item.parent_table}' or configure a default."
                        inv_val = None
                    else:
                        msg = (
                            f"Foreign key '{fk_item.child_column}' in table '{fk_item.child_table}' could not be resolved: "
                            f"'{ref_key}' not found in database table '{fk_item.parent_table}'."
                        )
                        corr = f"Ensure parent record '{ref_key}' exists in '{fk_item.parent_table}' or insert parent table first."
                        inv_val = str(ref_key)

                    err = RowValidationError(
                        rowNumber=0,
                        tableName=fk_item.child_table,
                        columnName=fk_item.child_column,
                        sourceColumn=inv_val,
                        invalidValue=inv_val,
                        errorType="UNRESOLVED_FOREIGN_KEY",
                        message=msg,
                        recommendedCorrection=corr,
                        severity="ERROR" if is_req else "WARNING"
                    )
                    if is_req:
                        errors.append(err)
                    else:
                        warnings.append(err)

                elif fk_item.status == "MISSING" and is_req:
                    unresolved_fks_count += 1
                    msg = (
                        f"Required foreign key '{fk_item.child_column}' in table '{fk_item.child_table}' "
                        f"referencing '{fk_item.parent_table}' is missing identifying name in source data."
                    )
                    errors.append(RowValidationError(
                        rowNumber=0,
                        tableName=fk_item.child_table,
                        columnName=fk_item.child_column,
                        sourceColumn=None,
                        invalidValue=None,
                        errorType="MISSING_FOREIGN_KEY_NAME",
                        message=msg,
                        recommendedCorrection=f"Provide identifying name in source data to resolve '{fk_item.parent_table}' ID.",
                        severity="ERROR"
                    ))

        # Retain as blocking errors: all Customer errors, plus any unresolved required foreign keys on active target tables.
        # Other non-customer findings are retained as warnings for visibility.
        customer_errors: List[RowValidationError] = []
        non_customer_errors: List[RowValidationError] = []
        for error in errors:
            normalized_table = str(error.table_name or "").lower().replace("dbo.", "").strip()
            is_unresolved_req_fk = error.error_type in (
                "UNRESOLVED_REQUIRED_FOREIGN_KEY",
                "REQUIRED_FOREIGN_KEY_UNMAPPED",
                "MISSING_REQUIRED_FOREIGN_KEY",
                "AMBIGUOUS_FOREIGN_KEY",
                "MISSING_FOREIGN_KEY_NAME",
                "UNRESOLVED_FOREIGN_KEY"
            ) and error.severity == "ERROR"
            if normalized_table == "customer" or is_unresolved_req_fk:
                customer_errors.append(error)
            else:
                non_customer_errors.append(error)
        if non_customer_errors:
            warnings.extend(non_customer_errors)
        errors = customer_errors
        invalid_row_set = {
            int(error.row_number)
            for error in customer_errors
            if getattr(error, "row_number", 0) and int(error.row_number) > 0
        }

        invalid_rows_count = len(invalid_row_set)
        valid_rows_count = max(0, total_rows - invalid_rows_count)
        can_execute = len(errors) == 0

        logger.info(
            f"[VALIDATOR] Validation complete: {total_rows} total rows, "
            f"{valid_rows_count} valid, {invalid_rows_count} invalid, "
            f"{len(errors)} errors, {len(warnings)} warnings."
        )

        return ValidationSummary(
            totalRows=total_rows,
            validRows=valid_rows_count,
            invalidRows=invalid_rows_count,
            warningsCount=len(warnings),
            matchedColumnsCount=matched_cols_count,
            unmatchedColumnsCount=unmatched_cols_count,
            unresolvedForeignKeysCount=unresolved_fks_count,
            errors=errors,
            warnings=warnings,
            circularDependencies=[w.message for w in warnings if w.error_type == "CIRCULAR_DEPENDENCY"],
            canExecute=can_execute
        )

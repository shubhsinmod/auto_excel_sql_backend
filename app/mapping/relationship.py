from typing import List, Dict, Any, Optional, Set, Tuple
from sqlalchemy import text, Engine
from app.models.schema import DatabaseSchemaMetadata
from app.models.migration import ValueResolutionResult, ColumnMappingResult
from app.mapping.matcher import normalize_string
from app.core.logging import logger


def quote_identifier(name: str, dialect_name: str) -> str:
    """Quotes table or column identifiers appropriately for SQL Server, MySQL, Postgres, SQLite."""
    if dialect_name == "mssql":
        return f"[{name}]"
    elif dialect_name in ("mysql", "mariadb"):
        return f"`{name}`"
    else:
        return f'"{name}"'


class ForeignKeyResolver:
    """
    Batch foreign key & value resolution engine.
    Fetches reference tables in batch (1 query per table) and resolves Excel values in memory.
    Zero N+1 queries.
    """

    @staticmethod
    def resolve_foreign_key_values(
        engine: Engine,
        schema: DatabaseSchemaMetadata,
        sample_rows: List[Dict[str, Any]],
        column_mappings: List[ColumnMappingResult]
    ) -> List[ValueResolutionResult]:
        """
        Resolves foreign key references and human-readable Excel values against parent database tables.
        """
        results: List[ValueResolutionResult] = []
        dialect_name = engine.name

        # 1. Find all foreign key target tables from database schema
        # Also include tables inferred from FK-like columns (e.g., Customer_ID -> Customer)
        ref_tables: Set[str] = set()
        schema_table_names = {t.name: t for t in schema.tables}

        for table in schema.tables:
            for fk in table.foreign_keys:
                if fk.target_table in schema_table_names:
                    ref_tables.add(fk.target_table)

        # Inferred relationships when no explicit FKs are declared
        if not ref_tables:
            for table in schema.tables:
                for col in table.columns:
                    col_norm = normalize_string(col.name)
                    for candidate_tbl in schema.tables:
                        if candidate_tbl.name != table.name:
                            tbl_norm = normalize_string(candidate_tbl.name)
                            if col_norm in (f"{tbl_norm}id", f"{tbl_norm}_id") or (col.name.endswith("_ID") and col.name[:-3].lower() == candidate_tbl.name.lower()):
                                ref_tables.add(candidate_tbl.name)

        if not ref_tables or not sample_rows:
            return results

        # 2. Batch load reference table records into memory (up to 1,000 reference rows per table)
        ref_maps: Dict[str, Dict[str, str]] = {}

        with engine.connect() as conn:
            for ref_table in ref_tables:
                try:
                    logger.info(f"Batch loading reference table '{ref_table}' for FK resolution...")
                    quoted_tbl = quote_identifier(ref_table, dialect_name)
                    
                    # Dialect-safe TOP / LIMIT query
                    if dialect_name == "mssql":
                        query_str = f"SELECT TOP 1000 * FROM {quoted_tbl}"
                    else:
                        query_str = f"SELECT * FROM {quoted_tbl} LIMIT 1000"

                    res = conn.execute(text(query_str))
                    rows = res.mappings().all()

                    val_to_id: Dict[str, str] = {}

                    for r in rows:
                        r_dict = dict(r)
                        if not r_dict:
                            continue
                        # Identify Primary Key column (ends with '_id', equals 'id', or first col)
                        pk_col = next((k for k in r_dict.keys() if k.lower().endswith("_id") or k.lower() == "id"), list(r_dict.keys())[0])
                        resolved_id = str(r_dict[pk_col])

                        # Map primary key value itself
                        val_to_id[normalize_string(resolved_id)] = resolved_id

                        # Map all textual/name attributes to the resolved ID
                        for col_k, col_v in r_dict.items():
                            if col_v is not None and col_k != pk_col:
                                val_to_id[normalize_string(str(col_v))] = resolved_id

                    ref_maps[ref_table] = val_to_id
                except Exception as e:
                    logger.warning(f"Could not load reference table '{ref_table}': {str(e)}")

        # 3. Extract unique values per column from sample Excel rows
        col_unique_vals: Dict[str, Set[str]] = {}
        for row in sample_rows:
            for col_name, val in row.items():
                if val is not None and str(val).strip() != "":
                    if col_name not in col_unique_vals:
                        col_unique_vals[col_name] = set()
                    col_unique_vals[col_name].add(str(val).strip())

        # 4. Resolve FK values against mapped target reference tables
        for mapping in column_mappings:
            if mapping.status != "MATCHED":
                continue

            excel_col = mapping.excel_column
            target_table = mapping.database_table
            target_col = mapping.database_column

            # Check if this table/column is linked to a FK reference
            target_ref_table = None
            target_pk_col = None

            table_meta = schema_table_names.get(target_table)
            if table_meta:
                for fk in table_meta.foreign_keys:
                    if fk.constrained_column == target_col:
                        target_ref_table = fk.target_table
                        target_pk_col = fk.target_column
                        break

            # If the column is directly in a reference table or FK linked
            effective_ref_table = target_ref_table if target_ref_table else (target_table if target_table in ref_maps else None)

            if effective_ref_table and effective_ref_table in ref_maps:
                val_map = ref_maps[effective_ref_table]
                unique_excel_vals = col_unique_vals.get(excel_col, set())

                for ex_val in list(unique_excel_vals)[:15]:  # Process sample unique values
                    norm_val = normalize_string(ex_val)
                    if norm_val in val_map:
                        resolved_id = val_map[norm_val]
                        results.append(ValueResolutionResult(
                            excelColumn=excel_col,
                            excelValue=ex_val,
                            referenceTable=effective_ref_table,
                            targetColumn=target_pk_col or target_col,
                            resolvedId=resolved_id,
                            status="MATCHED"
                        ))
                    else:
                        results.append(ValueResolutionResult(
                            excelColumn=excel_col,
                            excelValue=ex_val,
                            referenceTable=effective_ref_table,
                            targetColumn=target_pk_col or target_col,
                            resolvedId=None,
                            status="NOT_FOUND"
                        ))

        logger.info(f"Foreign key value resolution complete: {len(results)} value resolutions generated.")
        return results

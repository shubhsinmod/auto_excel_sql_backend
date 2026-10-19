import time
import threading
from typing import List, Dict, Any, Optional, Tuple, Set
from sqlalchemy import inspect, Engine, text
from app.core.logging import logger
from app.models.schema import (
    ColumnMetadata,
    TableMetadata,
    ForeignKeyInfo,
    DatabaseSchemaMetadata
)

# Thread-safe in-memory schema metadata cache
_SCHEMA_CACHE_LOCK = threading.Lock()
_SCHEMA_CACHE: Dict[str, Tuple[float, DatabaseSchemaMetadata]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes cache TTL


def clear_schema_cache(cache_key: Optional[str] = None):
    """Clears either a specific cached schema or all cached schemas."""
    with _SCHEMA_CACHE_LOCK:
        if cache_key:
            _SCHEMA_CACHE.pop(cache_key, None)
        else:
            _SCHEMA_CACHE.clear()
        logger.info("[DB] Schema cache cleared.")


def _get_cache_key(engine: Engine) -> str:
    try:
        return engine.url.render_as_string(hide_password=True)
    except Exception:
        return str(engine.url)


def _extract_mssql_catalog_metadata(engine: Engine) -> DatabaseSchemaMetadata:
    """
    High-performance SQL Server metadata discovery using system catalog views.
    Executes 4 batch queries instead of hundreds of individual table inspector queries,
    reducing introspection from ~60s to < 200ms.
    """
    db_name = engine.url.database or "default"
    db_type = "mssql"

    with engine.connect() as conn:
        # 1. Fetch all user tables and schemas
        tables_sql = text("""
            SELECT s.name AS schema_name, t.name AS table_name
            FROM sys.tables t
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE t.is_ms_shipped = 0
            ORDER BY s.name, t.name
        """)
        table_rows = conn.execute(tables_sql).mappings().all()

        if not table_rows:
            return DatabaseSchemaMetadata(database_name=db_name, db_type=db_type, tables=[])

        # 2. Fetch all columns with types, nullability, identity, defaults, lengths
        columns_sql = text("""
            SELECT 
                s.name AS schema_name,
                t.name AS table_name,
                c.name AS column_name,
                ty.name AS data_type,
                c.max_length,
                c.is_nullable,
                c.is_identity,
                dc.definition AS default_value,
                c.column_id
            FROM sys.columns c
            INNER JOIN sys.tables t ON c.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.types ty ON c.user_type_id = ty.user_type_id
            LEFT JOIN sys.default_constraints dc ON c.default_object_id = dc.object_id
            WHERE t.is_ms_shipped = 0
            ORDER BY s.name, t.name, c.column_id
        """)
        col_rows = conn.execute(columns_sql).mappings().all()

        # 3. Fetch primary keys and unique constraints
        pk_uq_sql = text("""
            SELECT 
                s.name AS schema_name,
                t.name AS table_name,
                c.name AS column_name,
                i.is_primary_key,
                i.is_unique_constraint
            FROM sys.indexes i
            INNER JOIN sys.tables t ON i.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            INNER JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            INNER JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE t.is_ms_shipped = 0 AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1)
        """)
        pk_uq_rows = conn.execute(pk_uq_sql).mappings().all()

        # 4. Fetch all foreign keys
        fk_sql = text("""
            SELECT 
                s_parent.name AS parent_schema,
                t_parent.name AS parent_table,
                c_parent.name AS parent_column,
                s_ref.name AS ref_schema,
                t_ref.name AS ref_table,
                c_ref.name AS ref_column
            FROM sys.foreign_key_columns fkc
            INNER JOIN sys.tables t_parent ON fkc.parent_object_id = t_parent.object_id
            INNER JOIN sys.schemas s_parent ON t_parent.schema_id = s_parent.schema_id
            INNER JOIN sys.columns c_parent ON fkc.parent_object_id = c_parent.object_id AND fkc.parent_column_id = c_parent.column_id
            INNER JOIN sys.tables t_ref ON fkc.referenced_object_id = t_ref.object_id
            INNER JOIN sys.schemas s_ref ON t_ref.schema_id = s_ref.schema_id
            INNER JOIN sys.columns c_ref ON fkc.referenced_object_id = c_ref.object_id AND fkc.referenced_column_id = c_ref.column_id
            WHERE t_parent.is_ms_shipped = 0
        """)
        fk_rows = conn.execute(fk_sql).mappings().all()

    # Index primary keys and unique constraints: (table_name, column_name) -> (is_pk, is_unique)
    pk_map: Dict[str, Set[str]] = {}
    unique_map: Dict[str, Set[str]] = {}
    for r in pk_uq_rows:
        t_name = str(r["table_name"])
        c_name = str(r["column_name"])
        if r.get("is_primary_key"):
            pk_map.setdefault(t_name, set()).add(c_name)
        if r.get("is_unique_constraint"):
            unique_map.setdefault(t_name, set()).add(c_name)

    # Index foreign keys: table_name -> List[ForeignKeyInfo]
    fk_map: Dict[str, List[ForeignKeyInfo]] = {}
    for r in fk_rows:
        t_name = str(r["parent_table"])
        fk_info = ForeignKeyInfo(
            constrained_column=str(r["parent_column"]),
            target_table=str(r["ref_table"]),
            target_column=str(r["ref_column"]),
            target_schema=str(r.get("ref_schema") or "dbo")
        )
        fk_map.setdefault(t_name, []).append(fk_info)

    # Group columns by table_name
    col_map: Dict[str, List[ColumnMetadata]] = {}
    for r in col_rows:
        t_name = str(r["table_name"])
        c_name = str(r["column_name"])
        is_pk = c_name in pk_map.get(t_name, set())
        is_unique = c_name in unique_map.get(t_name, set())
        is_ident = bool(r.get("is_identity"))
        is_null = bool(r.get("is_nullable"))
        data_type = str(r.get("data_type") or "")
        default_val = str(r["default_value"]) if r.get("default_value") is not None else None
        max_len = int(r["max_length"]) if r.get("max_length") is not None else None

        col_fks = [fk for fk in fk_map.get(t_name, []) if fk.constrained_column == c_name]

        col_meta = ColumnMetadata(
            name=c_name,
            type=data_type,
            nullable=is_null,
            primary_key=is_pk,
            identity=is_ident,
            default=default_val,
            unique=is_unique,
            foreign_keys=col_fks,
            max_length=max_len
        )
        col_map.setdefault(t_name, []).append(col_meta)

    # Assemble tables
    tables_metadata: List[TableMetadata] = []
    for r in table_rows:
        t_name = str(r["table_name"])
        s_name = str(r.get("schema_name") or "dbo")
        table_meta = TableMetadata(
            name=t_name,
            schema_name=s_name,
            columns=col_map.get(t_name, []),
            primary_keys=list(pk_map.get(t_name, set())),
            foreign_keys=fk_map.get(t_name, [])
        )
        tables_metadata.append(table_meta)

    return DatabaseSchemaMetadata(
        database_name=db_name,
        db_type=db_type,
        tables=tables_metadata
    )


def _extract_inspector_metadata(engine: Engine) -> DatabaseSchemaMetadata:
    """
    Standard introspection using SQLAlchemy inspector.
    Used for SQLite, PostgreSQL, MySQL, or when catalog queries fail.
    """
    inspector = inspect(engine)
    db_name = engine.url.database or "default"
    db_type = engine.name

    table_names = inspector.get_table_names()
    tables_metadata: List[TableMetadata] = []
    default_schema = "public" if db_type in ("postgresql", "postgres") else ("dbo" if db_type == "mssql" else "public")

    for table_name in table_names:
        pk_constraint = inspector.get_pk_constraint(table_name)
        primary_key_cols = set(pk_constraint.get("constrained_columns", []))

        foreign_keys_list: List[ForeignKeyInfo] = []
        fk_constraints = inspector.get_foreign_keys(table_name)
        for fk in fk_constraints:
            referred_table = fk.get("referred_table")
            constrained_cols = fk.get("constrained_columns", [])
            referred_cols = fk.get("referred_columns", [])
            referred_schema = fk.get("referred_schema") or default_schema

            for col, ref_col in zip(constrained_cols, referred_cols):
                fk_info = ForeignKeyInfo(
                    constrained_column=col,
                    target_table=referred_table,
                    target_column=ref_col,
                    target_schema=referred_schema
                )
                foreign_keys_list.append(fk_info)

        unique_cols = set()
        try:
            unique_constraints = inspector.get_unique_constraints(table_name)
            for uq in unique_constraints:
                for col in uq.get("column_names", []):
                    unique_cols.add(col)
        except Exception:
            pass

        raw_columns = inspector.get_columns(table_name)
        columns_metadata: List[ColumnMetadata] = []

        for col in raw_columns:
            col_name = col.get("name")
            is_pk = col_name in primary_key_cols
            is_autoincrement = col.get("autoincrement", False)
            identity = is_autoincrement is True or is_autoincrement == "auto"

            col_fks = [fk for fk in foreign_keys_list if fk.constrained_column == col_name]

            column_meta = ColumnMetadata(
                name=col_name,
                type=str(col.get("type")),
                nullable=col.get("nullable", True),
                primary_key=is_pk,
                identity=identity,
                default=str(col.get("default")) if col.get("default") is not None else None,
                unique=(col_name in unique_cols),
                foreign_keys=col_fks
            )
            columns_metadata.append(column_meta)

        table_meta = TableMetadata(
            name=table_name,
            schema_name=default_schema,
            columns=columns_metadata,
            primary_keys=list(primary_key_cols),
            foreign_keys=foreign_keys_list
        )
        tables_metadata.append(table_meta)

    return DatabaseSchemaMetadata(
        database_name=db_name,
        db_type=db_type,
        tables=tables_metadata
    )


def extract_database_metadata(engine: Engine, use_cache: bool = True) -> DatabaseSchemaMetadata:
    """
    Extracts database schema metadata with caching and catalog optimization.
    - Caches metadata in memory per connection URL for fast lookups.
    - Optimizes SQL Server metadata retrieval using catalog queries (< 200ms vs ~60s).
    - Falls back safely to SQLAlchemy inspector for other dialects or on error.
    """
    cache_key = _get_cache_key(engine)

    if use_cache:
        with _SCHEMA_CACHE_LOCK:
            if cache_key in _SCHEMA_CACHE:
                cached_time, cached_meta = _SCHEMA_CACHE[cache_key]
                if time.time() - cached_time < CACHE_TTL_SECONDS:
                    logger.debug(f"[DB] Returning cached schema metadata for {cache_key}")
                    return cached_meta

    t_start = time.time()
    logger.info(f"[DB] Schema introspection START | dialect={engine.name}")

    result: Optional[DatabaseSchemaMetadata] = None

    if engine.name == "mssql":
        try:
            logger.info("[DB] Attempting fast catalog queries for SQL Server...")
            result = _extract_mssql_catalog_metadata(engine)
            logger.info(f"[DB] Fast SQL Server catalog introspection completed with {len(result.tables)} tables.")
        except Exception as catalog_err:
            logger.warning(f"[DB] Fast catalog query failed ({catalog_err}), falling back to standard inspector.")
            result = None

    if result is None:
        result = _extract_inspector_metadata(engine)

    t_end = time.time()
    logger.info(f"[DB] Schema introspection END | {t_end - t_start:.3f}s | tables={len(result.tables)}")

    if use_cache:
        with _SCHEMA_CACHE_LOCK:
            _SCHEMA_CACHE[cache_key] = (time.time(), result)

    return result
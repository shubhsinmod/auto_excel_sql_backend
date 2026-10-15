"""
Unit Tests for Relevant Table Processing and Dynamic Table Role Classification.

Verifies:
1. Dynamic Relevant Table Discovery: Identifies destination tables based on Excel column mapping evidence.
2. Tables are classified into TableRole:
   - TARGET_INSERT: Tables receiving new Excel records.
   - LOOKUP_ONLY: Reference/lookup tables used to resolve foreign keys (never inserted into).
   - DEPENDENCY_INSERT: Required parent entity tables that need insertion when missing.
   - SKIP: Irrelevant/unrelated tables that have no mapping evidence and no FK references.
3. Minimal dependency graph: Unrelated tables (e.g. AuditLog, DeliveryBoys, VendorSuppliers) are completely SKIPPED and NEVER queried.
4. Lookup-only tables (e.g. ItemsMaster for OrderLineItems.Item_ID) are queried with SELECT for foreign key resolution, but NEVER receive INSERT statements.
5. Planned operations summary and Step3 response explicitly report:
   - tableRoles
   - skippedTables with reasons
   - targetInsertTables
   - lookupOnlyTables
   - dependencyInsertTables
"""

import pytest
import pandas as pd
from unittest.mock import MagicMock
from sqlalchemy import text

from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata, ForeignKeyInfo
from app.models.migration import TableRole, ColumnMappingResult
from app.mapping.matcher import ColumnMatcher
from app.migration.preview import MigrationPreviewEngine
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship


@pytest.fixture
def enterprise_pos_schema():
    """
    Schema with relevant transactional tables (Order, OrderLineItems),
    lookup-only master table (ItemsMaster),
    and several completely unrelated tables (AuditLog, DeliveryBoys, VendorSuppliers, ArchiveBills).
    """
    # 1. Target table: Order
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Invoice_No", type="VARCHAR(50)", nullable=False),
        ColumnMetadata(name="Order_Date", type="DATETIME", nullable=False),
        ColumnMetadata(name="Total_Amount", type="DECIMAL(10,2)", nullable=False),
    ]

    # 2. Target table: OrderLineItems (references ItemsMaster via Item_ID)
    order_line_cols = [
        ColumnMetadata(name="Line_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_ID", type="INT", nullable=False),
        ColumnMetadata(name="Item_ID", type="INT", nullable=False),
        ColumnMetadata(name="Description", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="Quantity", type="INT", nullable=False),
        ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False),
    ]
    order_line_fks = [
        ForeignKeyInfo(constrained_column="Order_ID", target_table="Order", target_column="Order_ID"),
        ForeignKeyInfo(constrained_column="Item_ID", target_table="ItemsMaster", target_column="Item_ID"),
    ]

    # 3. Lookup-only master table: ItemsMaster (read-only for name-to-ID lookup)
    items_master_cols = [
        ColumnMetadata(name="ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Item_ID", type="INT", nullable=False),
        ColumnMetadata(name="Item", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False),
    ]

    # 4. Unrelated table: AuditLog
    audit_cols = [
        ColumnMetadata(name="Log_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="User_Action", type="VARCHAR(100)", nullable=False),
        ColumnMetadata(name="Timestamp", type="DATETIME", nullable=False),
        ColumnMetadata(name="Details", type="VARCHAR(500)", nullable=True),
    ]

    # 5. Unrelated table: DeliveryBoys
    delivery_boys_cols = [
        ColumnMetadata(name="DeliveryBoy_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="DeliveryBoy_Name", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="Vehicle_Number", type="VARCHAR(50)", nullable=True),
        ColumnMetadata(name="Phone", type="VARCHAR(20)", nullable=False),
    ]

    # 6. Unrelated table: VendorSuppliers
    vendor_cols = [
        ColumnMetadata(name="Supplier_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Supplier_Name", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="Tax_Registration", type="VARCHAR(50)", nullable=True),
    ]

    # 7. Unrelated table: ArchiveBills
    archive_cols = [
        ColumnMetadata(name="Archive_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Old_Bill_No", type="VARCHAR(50)", nullable=False),
        ColumnMetadata(name="Archived_On", type="DATETIME", nullable=False),
    ]

    return DatabaseSchemaMetadata(
        database_name="EnterprisePOS",
        db_type="mssql",
        tables=[
            TableMetadata(name="Order", columns=order_cols),
            TableMetadata(name="OrderLineItems", columns=order_line_cols, foreign_keys=order_line_fks),
            TableMetadata(name="ItemsMaster", columns=items_master_cols),
            TableMetadata(name="AuditLog", columns=audit_cols),
            TableMetadata(name="DeliveryBoys", columns=delivery_boys_cols),
            TableMetadata(name="VendorSuppliers", columns=vendor_cols),
            TableMetadata(name="ArchiveBills", columns=archive_cols),
        ]
    )


class QuerySpyConnection:
    """Mock connection that records all executed SQL queries and returns empty / sample results."""
    def __init__(self, executed_queries_list):
        self.executed_queries = executed_queries_list

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute(self, statement, *args, **kwargs):
        sql_str = str(statement)
        self.executed_queries.append(sql_str)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [
            {"Item_ID": 22, "Item": "Masala Dosa", "Item_Price": 46.296},
            {"Item_ID": 59, "Item": "Gulab Jamun (2pc)", "Item_Price": 27.778},
        ]
        return mock_result


class QuerySpyEngine:
    """Mock SQLAlchemy engine that records executed queries."""
    def __init__(self):
        self.name = "mssql"
        self.executed_queries = []

    def connect(self):
        return QuerySpyConnection(self.executed_queries)


def test_dynamic_relevant_table_discovery_and_role_classification(enterprise_pos_schema):
    """
    Given an uploaded Excel file with Order and OrderLineItems data:
    1. Order and OrderLineItems must be classified as TARGET_INSERT.
    2. ItemsMaster must be classified as LOOKUP_ONLY (because Item_ID references it).
    3. AuditLog, DeliveryBoys, VendorSuppliers, ArchiveBills must be classified as SKIP.
    4. Unrelated tables must NEVER be queried.
    """
    # Excel dataframe with order and line item data
    df = pd.DataFrame([
        {
            "invoice_no": "INV-1001",
            "order_date": "2026-10-12 12:00:00",
            "total_amount": "125.00",
            "item_name": "Masala Dosa",
            "quantity": 2,
            "item_price": 50.00,
        },
        {
            "invoice_no": "INV-1001",
            "order_date": "2026-10-12 12:00:00",
            "total_amount": "125.00",
            "item_name": "Gulab Jamun (2pc)",
            "quantity": 1,
            "item_price": 25.00,
        }
    ])

    column_mappings = ColumnMatcher.match_columns(
        excel_columns=df.columns.tolist(),
        schema=enterprise_pos_schema
    )

    spy_engine = QuerySpyEngine()

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="daily_orders.xlsx",
        schema=enterprise_pos_schema,
        column_mappings=column_mappings,
        engine=spy_engine
    )

    planned_ops = preview_res.planned_operations
    assert planned_ops is not None

    table_roles = planned_ops.table_roles
    assert table_roles["Order"] == TableRole.TARGET_INSERT.value
    assert table_roles["OrderLineItems"] == TableRole.TARGET_INSERT.value
    assert table_roles["ItemsMaster"] == TableRole.LOOKUP_ONLY.value

    # Verify unrelated tables are SKIPPED
    assert table_roles["AuditLog"] == TableRole.SKIP.value
    assert table_roles["DeliveryBoys"] == TableRole.SKIP.value
    assert table_roles["VendorSuppliers"] == TableRole.SKIP.value
    assert table_roles["ArchiveBills"] == TableRole.SKIP.value

    # Verify skipped table explanations
    assert "AuditLog" in planned_ops.skipped_tables
    assert "DeliveryBoys" in planned_ops.skipped_tables
    assert "VendorSuppliers" in planned_ops.skipped_tables
    assert "ArchiveBills" in planned_ops.skipped_tables

    # Verify targetInsertTables and lookupOnlyTables lists
    assert "Order" in planned_ops.target_insert_tables
    assert "OrderLineItems" in planned_ops.target_insert_tables
    assert "ItemsMaster" in planned_ops.lookup_only_tables
    assert "AuditLog" not in planned_ops.target_insert_tables
    assert "AuditLog" not in planned_ops.lookup_only_tables

    # Verify that skipped tables were NEVER queried by the database engine
    all_queries = " ".join(spy_engine.executed_queries).lower()
    assert "auditlog" not in all_queries
    assert "deliveryboys" not in all_queries
    assert "vendorsuppliers" not in all_queries
    assert "archivebills" not in all_queries

    # Verify that ItemsMaster was queried for lookup
    assert "itemsmaster" in all_queries

    # Verify that planned inserts do NOT include ItemsMaster (lookup-only tables must NOT be inserted into)
    assert "ItemsMaster" not in planned_ops.planned_inserts_by_table or planned_ops.planned_inserts_by_table.get("ItemsMaster", 0) == 0


def test_dependency_order_preserves_parent_before_child(enterprise_pos_schema):
    """
    Parent tables must be processed before dependent child tables (Order before OrderLineItems).
    """
    df = pd.DataFrame([
        {
            "invoice_no": "INV-2001",
            "order_date": "2026-10-12 13:00:00",
            "total_amount": "50.00",
            "item_name": "Masala Dosa",
            "quantity": 1,
            "item_price": 50.00,
        }
    ])

    column_mappings = ColumnMatcher.match_columns(
        excel_columns=df.columns.tolist(),
        schema=enterprise_pos_schema
    )

    spy_engine = QuerySpyEngine()
    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="single_order.xlsx",
        schema=enterprise_pos_schema,
        column_mappings=column_mappings,
        engine=spy_engine
    )

    execution_order = preview_res.execution_order
    # Order must precede OrderLineItems
    assert execution_order.index("Order") < execution_order.index("OrderLineItems")
    # Lookup-only or skipped tables should NOT be in the execution insert order
    assert "ItemsMaster" not in execution_order
    assert "AuditLog" not in execution_order
    assert "DeliveryBoys" not in execution_order


def test_parent_records_cache_reuse_eliminates_duplicate_queries(enterprise_pos_schema):
    """
    Verify performance optimization: When ItemsMaster is queried in Stage 2
    (load_parent_records), it is NOT re-queried redundantly in Stage 3 (existing record inspection).
    Query count for ItemsMaster must be exactly 1.
    """
    df = pd.DataFrame([
        {
            "invoice_no": "INV-3001",
            "order_date": "2026-10-12 14:00:00",
            "total_amount": "80.00",
            "item_name": "Masala Dosa",
            "quantity": 1,
            "item_price": 50.00,
        }
    ])

    column_mappings = ColumnMatcher.match_columns(
        excel_columns=df.columns.tolist(),
        schema=enterprise_pos_schema
    )

    spy_engine = QuerySpyEngine()
    MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="cached_lookup.xlsx",
        schema=enterprise_pos_schema,
        column_mappings=column_mappings,
        engine=spy_engine
    )

    itemsmaster_queries = [q for q in spy_engine.executed_queries if "itemsmaster" in q.lower()]
    # Must only be queried once across the entire preview lifecycle
    assert len(itemsmaster_queries) == 1, f"Expected 1 ItemsMaster query, got {len(itemsmaster_queries)}: {itemsmaster_queries}"


def test_lookup_only_tables_receive_zero_inserts_in_execution_engine(enterprise_pos_schema):
    """
    Verify that in MigrationEngine execution, LOOKUP_ONLY tables (such as ItemsMaster and Outlets)
    are strictly excluded from active_target_tables, receiving zero database writes.
    """
    from app.migration.engine import MigrationEngine
    from app.services.excel_service import excel_service
    from app.services.database_service import database_service
    from unittest.mock import patch

    df = pd.DataFrame([
        {
            "invoice_no": "INV-4001",
            "order_date": "2026-10-12 15:00:00",
            "total_amount": "100.00",
            "item_name": "Masala Dosa",
            "quantity": 2,
            "item_price": 50.00,
        }
    ])

    column_mappings = ColumnMatcher.match_columns(
        excel_columns=df.columns.tolist(),
        schema=enterprise_pos_schema
    )

    spy_engine = QuerySpyEngine()
    job_id = "test-job-relevant-tables"

    with patch.object(excel_service, "get_upload_session", return_value={"filePath": "dummy.xlsx", "sheetName": "Sheet1"}), \
         patch("pandas.io.common.file_exists", return_value=True), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch.object(database_service, "get_schema_metadata", return_value=enterprise_pos_schema), \
         patch("app.migration.engine.get_engine", return_value=spy_engine), \
         patch("app.migration.engine.transaction_manager") as mock_tm:

        # Let transaction_manager directly execute the function with spy_engine connection
        def fake_exec(*args, **kwargs):
            fn = kwargs.get("execution_fn") or args[2]
            with spy_engine.connect() as conn:
                return fn(conn)
        mock_tm.execute_transactionally.side_effect = fake_exec

        exec_response = MigrationEngine.execute_migration(
            upload_id=job_id
        )

    # active_tables_ordered in engine must only contain Order and OrderLineItems
    migrated_tables = list(exec_response.tables.keys())
    assert "ItemsMaster" not in migrated_tables
    assert "AuditLog" not in migrated_tables
    assert "DeliveryBoys" not in migrated_tables

    # ItemsMaster must NEVER appear in any INSERT or UPDATE statements
    insert_queries = [q for q in spy_engine.executed_queries if "insert into" in q.lower()]
    for q in insert_queries:
        assert "itemsmaster" not in q.lower(), f"Unexpected INSERT into ItemsMaster: {q}"
        assert "outlets" not in q.lower(), f"Unexpected INSERT into Outlets: {q}"
        assert "auditlog" not in q.lower()

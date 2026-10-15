"""
Comprehensive regression tests for resolving Outlet_ID, Assigned_Outlet_ID, and Order_Outlet_ID:
Requirement 7 Scenarios:
1. Single outlet match: Source outlet name uniquely matches existing outlet record -> resolves Outlet_ID.
2. Value mapping reuse: Value mapper resolves outlet address/location, FK resolver reuses the matched record's Outlet_ID.
3. Outlet_ID resolved: Outlet_ID in dbo.Customer resolves to correct existing ID.
4. Assigned_Outlet_ID resolved: Assigned_Outlet_ID in dbo.Order resolves to correct existing ID.
5. Order_Outlet_ID resolved: Order_Outlet_ID in dbo.Order resolves to correct existing ID.
6. Multiple FKs same record: When Outlet_ID, Assigned_Outlet_ID, and Order_Outlet_ID represent the same outlet, all resolve to the same ID.
7. Multiple FKs different records: When Assigned_Outlet_ID references a different outlet than Order_Outlet_ID, each resolves to its respective ID.
8. Ambiguous match handling: Multiple matching outlet records -> flagged as AMBIGUOUS, not resolved.
9. Missing source value: Source has no outlet info -> marked UNRESOLVED with clear message, no DB query with 'None' or 'nan'.
10. SQL Preview accuracy: Resolved IDs appear in the generated SQL preview.
11. Read-only preview: Preview mode performs zero database writes.
"""

import pytest
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text

from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
from app.models.migration import ColumnMappingResult, ResolvedEntityRecord, ValueMappingItem
from app.migration.lookup_resolver import DynamicLookupResolver, LookupRelationship
from app.migration.preview import MigrationPreviewEngine
from app.migration.validator import MigrationValidator
from app.mapping.values import ValueMatcher


@pytest.fixture
def mock_schema():
    """Builds a schema mirroring BOPOS_Leons with Customer, Order, and Outlets tables."""
    outlets_cols = [
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR", max_length=100, nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Address", type="VARCHAR", max_length=200, nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Short_Code", type="VARCHAR", max_length=20, nullable=True, primary_key=False, identity=False),
    ]
    outlets_tbl = TableMetadata(
        name="Outlets",
        schema_name="dbo",
        columns=outlets_cols,
        primary_keys=["Outlet_ID"],
        foreign_keys=[]
    )

    cust_fk_outlet = ForeignKeyInfo(
        constrained_column="Outlet_ID",
        target_table="Outlets",
        target_column="Outlet_ID",
        target_schema="dbo"
    )
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Location", type="VARCHAR", max_length=200, nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, primary_key=False, identity=False, foreign_keys=[cust_fk_outlet]),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[cust_fk_outlet]
    )

    order_fk_cust = ForeignKeyInfo(
        constrained_column="Customer_ID",
        target_table="Customer",
        target_column="Customer_ID",
        target_schema="dbo"
    )
    order_fk_outlet = ForeignKeyInfo(
        constrained_column="Order_Outlet_ID",
        target_table="Outlets",
        target_column="Outlet_ID",
        target_schema="dbo"
    )
    order_fk_assigned = ForeignKeyInfo(
        constrained_column="Assigned_Outlet_ID",
        target_table="Outlets",
        target_column="Outlet_ID",
        target_schema="dbo"
    )
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=False, identity=False, foreign_keys=[order_fk_cust]),
        ColumnMetadata(name="Order_Outlet_ID", type="INT", nullable=False, primary_key=False, identity=False, foreign_keys=[order_fk_outlet]),
        ColumnMetadata(name="Assigned_Outlet_ID", type="INT", nullable=True, primary_key=False, identity=False, foreign_keys=[order_fk_assigned]),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False, primary_key=False, identity=False),
    ]
    order_tbl = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[order_fk_cust, order_fk_outlet, order_fk_assigned]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Leons",
        db_type="mssql",
        tables=[outlets_tbl, customer_tbl, order_tbl]
    )


@pytest.fixture
def test_db_engine():
    """In-memory SQLite engine simulating SQL Server Outlets, Customer, and Order tables."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Outlets (
                Outlet_ID INTEGER PRIMARY KEY,
                Outlet_Name TEXT NOT NULL,
                Address TEXT,
                Short_Code TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY,
                Customer_Name TEXT NOT NULL,
                Location TEXT,
                Outlet_ID INTEGER
            );
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY,
                Customer_ID INTEGER,
                Order_Outlet_ID INTEGER NOT NULL,
                Assigned_Outlet_ID INTEGER,
                Order_DateTime TEXT NOT NULL
            );
        """))

        # Seed realistic records
        conn.execute(text("""
            INSERT INTO Outlets (Outlet_ID, Outlet_Name, Address, Short_Code)
            VALUES (69, 'ANN', '210, 4th Ave, AC Block, Anna Nagar, Chennai, Tamil Nadu 600040', 'ANN');
        """))
        conn.execute(text("""
            INSERT INTO Outlets (Outlet_ID, Outlet_Name, Address, Short_Code)
            VALUES (42, 'Express Avenue', 'Express Avenue Mall, Royapettah, Chennai', 'EA');
        """))
        conn.execute(text("""
            INSERT INTO Customer (Customer_ID, Customer_Name, Location, Outlet_ID)
            VALUES (1, 'Prateek Singh', '14th Avenue, Chetpet, Chennai', 69);
        """))
    return engine


# -----------------------------------------------------------------------------
# Scenario 1: Single outlet match
# -----------------------------------------------------------------------------
def test_1_single_outlet_match():
    """Source outlet name uniquely matches existing outlet record -> resolves Outlet_ID."""
    outlets = [
        {"Outlet_ID": 69, "Outlet_Name": "ANN", "Short_Code": "ANN", "Address": "Anna Nagar"}
    ]
    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Customer",
        child_column="Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=outlets,
        excel_source_value="ANN"
    )
    assert res.status == "RESOLVED"
    assert res.matched_primary_key_value == 69
    assert res.confidence == 1.0
    assert res.record_data["Outlet_Name"] == "ANN"


# -----------------------------------------------------------------------------
# Scenario 2: Value mapping reuse
# -----------------------------------------------------------------------------
def test_2_customer_location_value_mapping_does_not_resolve_outlet_fk():
    """Customer.Location is not an outlet identity substitute."""
    outlets = [
        {"Outlet_ID": 69, "Outlet_Name": "ANN", "Short_Code": "ANN", "Address": "210, 4th Ave, AC Block, Anna Nagar"}
    ]
    vm = ValueMappingItem(
        excelColumn="restaurant_name",
        databaseTable="Customer",
        databaseColumn="Location",
        excelValue="Namaste",
        targetValue="14th Avenue, Chetpet, Chennai",
        status="MATCHED",
        confidence=0.95,
        matchedTable="Customer",
        matchedPrimaryKeyColumn="Customer_ID",
        matchedPrimaryKeyValue=1,
        matchedRecordData={"Customer_ID": 1, "Location": "14th Avenue, Chetpet, Chennai", "Outlet_ID": 69}
    )

    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Customer",
        child_column="Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        db_records=outlets,
        excel_source_value="Namaste",
        row_value_mappings=[vm]
    )
    assert res.status == "UNRESOLVED"
    assert res.matched_primary_key_value is None


# -----------------------------------------------------------------------------
# Scenario 3, 4, 5: Outlet_ID, Assigned_Outlet_ID, Order_Outlet_ID resolved
# -----------------------------------------------------------------------------
def test_3_4_5_outlet_fks_resolved_in_preview(mock_schema, test_db_engine):
    """
    Ensures Outlet_ID in Customer, and Order_Outlet_ID & Assigned_Outlet_ID in Order
    all resolve to 69 when restaurant_name explicitly identifies the existing ANN outlet.
    """
    df = pd.DataFrame([{
        "restaurant_name": "ANN",
        "customer_name": "Prateek",
        "date": "2026-07-01 12:00:00"
    }])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED", confidence=0.99),
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED", confidence=1.0),
    ]

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_order.csv",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    rows = preview_res.preview_rows

    # Customer.Outlet_ID
    cust_outlet_item = next((r for r in rows if r.target_table == "Customer" and r.target_column == "Outlet_ID"), None)
    assert cust_outlet_item is not None
    assert cust_outlet_item.final_value == "69"
    assert cust_outlet_item.value_source == "DATABASE_LOOKUP"
    assert cust_outlet_item.validation_status == "VALID"

    # Order.Order_Outlet_ID
    order_outlet_item = next((r for r in rows if r.target_table == "Order" and r.target_column == "Order_Outlet_ID"), None)
    assert order_outlet_item is not None
    assert order_outlet_item.final_value == "69"
    assert order_outlet_item.value_source == "DATABASE_LOOKUP"
    assert order_outlet_item.validation_status == "VALID"

    # Order.Assigned_Outlet_ID
    assigned_outlet_item = next((r for r in rows if r.target_table == "Order" and r.target_column == "Assigned_Outlet_ID"), None)
    assert assigned_outlet_item is not None
    assert assigned_outlet_item.final_value == "69"
    assert assigned_outlet_item.value_source == "DATABASE_LOOKUP"
    assert assigned_outlet_item.validation_status == "VALID"


# -----------------------------------------------------------------------------
# Scenario 6: Multiple FKs referencing same outlet record
# -----------------------------------------------------------------------------
def test_6_multiple_fks_same_outlet_reuse():
    """When Outlet_ID, Assigned_Outlet_ID, and Order_Outlet_ID represent same outlet, all resolve to same ID."""
    row_resolved_entities = {
        "Outlets": ResolvedEntityRecord(
            status="RESOLVED",
            matchedTable="Outlets",
            matchedPrimaryKeyColumn="Outlet_ID",
            matchedPrimaryKeyValue=69,
            recordData={"Outlet_ID": 69, "Outlet_Name": "ANN"}
        )
    }

    # Order_Outlet_ID (no explicit source override)
    r1 = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Order",
        child_column="Order_Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        row_resolved_entities=row_resolved_entities
    )
    # Assigned_Outlet_ID (source is empty/NaN)
    r2 = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Order",
        child_column="Assigned_Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        excel_source_value=None,
        row_resolved_entities=row_resolved_entities
    )
    # Customer.Outlet_ID
    r3 = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Customer",
        child_column="Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        row_resolved_entities=row_resolved_entities
    )

    assert r1.matched_primary_key_value == 69
    assert r2.matched_primary_key_value == 69
    assert r3.matched_primary_key_value == 69


# -----------------------------------------------------------------------------
# Scenario 7: Multiple FKs different records (e.g. distinct assign_to)
# -----------------------------------------------------------------------------
def test_7_multiple_fks_different_outlets():
    """When Assigned_Outlet_ID references a different outlet than Order_Outlet_ID, each resolves independently."""
    outlets = [
        {"Outlet_ID": 69, "Outlet_Name": "ANN", "Short_Code": "ANN"},
        {"Outlet_ID": 42, "Outlet_Name": "Express Avenue", "Short_Code": "EA"},
    ]
    row_resolved_entities = {
        "Outlets": ResolvedEntityRecord(
            status="RESOLVED",
            matchedTable="Outlets",
            matchedPrimaryKeyColumn="Outlet_ID",
            matchedPrimaryKeyValue=69,
            recordData={"Outlet_ID": 69, "Outlet_Name": "ANN"}
        )
    }

    # Order_Outlet_ID resolves to ANN (69)
    r_order = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Order",
        child_column="Order_Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        excel_source_value="ANN",
        db_records=outlets,
        row_resolved_entities=row_resolved_entities
    )

    # Assigned_Outlet_ID specifies Express Avenue (42)
    r_assigned = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Order",
        child_column="Assigned_Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        excel_source_value="Express Avenue",
        db_records=outlets,
        row_resolved_entities=row_resolved_entities
    )

    assert r_order.matched_primary_key_value == 69
    assert r_assigned.matched_primary_key_value == 42
    assert r_assigned.matched_primary_key_value != r_order.matched_primary_key_value


# -----------------------------------------------------------------------------
# Scenario 8: Ambiguous match handling
# -----------------------------------------------------------------------------
def test_8_ambiguous_match_handling():
    """Multiple matching outlet records -> flagged as AMBIGUOUS, not resolved."""
    outlets = [
        {"Outlet_ID": 10, "Outlet_Name": "Chennai Central Branch"},
        {"Outlet_ID": 20, "Outlet_Name": "Chennai Central Express"},
    ]
    res = DynamicLookupResolver.resolve_foreign_key_record(
        child_table="Order",
        child_column="Order_Outlet_ID",
        parent_table="Outlets",
        parent_id_column="Outlet_ID",
        parent_name_column="Outlet_Name",
        excel_source_value="Chennai Central",
        db_records=outlets
    )
    assert res.status == "AMBIGUOUS"
    assert res.matched_primary_key_value is None
    assert len(res.candidates) >= 2


# -----------------------------------------------------------------------------
# Scenario 9: Missing source value handling
# -----------------------------------------------------------------------------
def test_9_missing_source_value_handling():
    """Source has no outlet info -> marked UNRESOLVED with clear message, no DB query with 'None' or 'nan'."""
    outlets = [
        {"Outlet_ID": 69, "Outlet_Name": "ANN"}
    ]
    for bad_val in [None, np.nan, "", "   ", "nan", "None", "null"]:
        res = DynamicLookupResolver.resolve_foreign_key_record(
            child_table="Order",
            child_column="Order_Outlet_ID",
            parent_table="Outlets",
            parent_id_column="Outlet_ID",
            parent_name_column="Outlet_Name",
            excel_source_value=bad_val,
            db_records=outlets
        )
        assert res.status == "UNRESOLVED"
        assert res.matched_primary_key_value is None
        assert "missing" in res.message.lower() or "not found" in res.message.lower()


# -----------------------------------------------------------------------------
# Scenario 10: SQL Preview accuracy
# -----------------------------------------------------------------------------
def test_10_sql_preview_accuracy(mock_schema, test_db_engine):
    """Resolved IDs appear accurately in the generated SQL preview rows."""
    df = pd.DataFrame([{
        "restaurant_name": "ANN",
        "customer_name": "Prateek",
        "date": "2026-07-01 12:00:00"
    }])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Customer", databaseColumn="Location", status="MATCHED", confidence=0.99),
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED", confidence=1.0),
    ]

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_order.csv",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    outlet_rows = [r for r in preview_res.preview_rows if r.target_column in ("Outlet_ID", "Order_Outlet_ID", "Assigned_Outlet_ID")]
    assert len(outlet_rows) == 3
    for r in outlet_rows:
        assert r.final_value == "69"
        assert r.value_source == "DATABASE_LOOKUP"
        assert r.validation_status == "VALID"


# -----------------------------------------------------------------------------
# Scenario 11: Read-only preview
# -----------------------------------------------------------------------------
def test_11_read_only_preview_performs_no_writes(mock_schema, test_db_engine):
    """Preview mode performs zero database writes."""
    with test_db_engine.connect() as conn:
        before_orders = conn.execute(text('SELECT COUNT(*) FROM "Order"')).scalar()
        before_custs = conn.execute(text('SELECT COUNT(*) FROM Customer')).scalar()
        before_outlets = conn.execute(text('SELECT COUNT(*) FROM Outlets')).scalar()

    df = pd.DataFrame([{
        "restaurant_name": "ANN",
        "customer_name": "New Customer",
        "date": "2026-07-01 12:00:00"
    }])

    mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Customer", databaseColumn="Location", status="MATCHED", confidence=0.99),
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED", confidence=1.0),
    ]

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="test_order.csv",
        schema=mock_schema,
        column_mappings=mappings,
        engine=test_db_engine
    )

    with test_db_engine.connect() as conn:
        after_orders = conn.execute(text('SELECT COUNT(*) FROM "Order"')).scalar()
        after_custs = conn.execute(text('SELECT COUNT(*) FROM Customer')).scalar()
        after_outlets = conn.execute(text('SELECT COUNT(*) FROM Outlets')).scalar()

    assert before_orders == after_orders == 0
    assert before_custs == after_custs == 1
    assert before_outlets == after_outlets == 2
    assert preview_res.planned_operations.statement == "This preview has not written any data to the database."

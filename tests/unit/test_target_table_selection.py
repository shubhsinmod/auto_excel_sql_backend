"""
Unit Tests for Target Table Selection and Excessive Table Inserts Prevention.

Verifies:
1. Mock 15-table schema where only 5 are genuine target / lookup tables:
   - Genuine Targets: Customer, Order, Bills, OrderLineItems
   - Genuine Lookup: Outlets
   - Incidental/Related Schema Tables (must be classified as SKIP):
     CashDeclarations, Customer_Centric_Vouchers, Employees, Item_Groups,
     OptiPro_Sync_Orders, Order_Pickups, Outlet_Centric_Vouchers, SMSQueue,
     Audit_Logs, PrintQueue
2. Only genuine target tables receive TableRole.TARGET_INSERT.
3. Lookup table receives TableRole.LOOKUP_ONLY (zero INSERT statements planned/executed).
4. All incidental tables matching generic timestamps or fuzzy noise receive TableRole.SKIP with clear reasons.
5. Migration Execution Report and PlannedOperationsSummary correctly separate:
   - targetTables
   - lookupTables
   - skippedTables with reasons
"""

import pytest
import pandas as pd
from unittest.mock import MagicMock
from sqlalchemy import create_engine, text

from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata, ForeignKeyInfo
from app.models.migration import TableRole, ColumnMappingResult
from app.mapping.matcher import ColumnMatcher
from app.migration.preview import MigrationPreviewEngine, classify_table_relevance


@pytest.fixture
def mock_15_table_schema():
    """Builds a representative 15-table enterprise schema."""
    # 1. Customer
    cust_cols = [
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False),
        ColumnMetadata(name="Mobile_Number", type="VARCHAR(20)", nullable=True),
        ColumnMetadata(name="Address", type="VARCHAR(255)", nullable=True),
    ]
    cust_tbl = TableMetadata(name="Customer", schema_name="dbo", columns=cust_cols, primary_keys=["Customer_ID"])

    # 2. Outlets (Lookup Only)
    out_cols = [
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Outlet_Name", type="VARCHAR(100)", nullable=False),
        ColumnMetadata(name="Address", type="VARCHAR(255)", nullable=True),
    ]
    out_tbl = TableMetadata(name="Outlets", schema_name="dbo", columns=out_cols, primary_keys=["Outlet_ID"])

    # 3. Order
    order_fk_cust = ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
    order_fk_outlet = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID")
    order_cols = [
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INT", nullable=False, foreign_keys=[order_fk_cust]),
        ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, foreign_keys=[order_fk_outlet]),
        ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False),
        ColumnMetadata(name="Order_Status", type="VARCHAR(50)", nullable=True),
        ColumnMetadata(name="Tax", type="DECIMAL(10,2)", nullable=True),
    ]
    order_tbl = TableMetadata(name="Order", schema_name="dbo", columns=order_cols, primary_keys=["Order_ID"], foreign_keys=[order_fk_cust, order_fk_outlet])

    # 4. Bills
    bill_fk_order = ForeignKeyInfo(constrained_column="Order_ID", target_table="Order", target_column="Order_ID")
    bill_cols = [
        ColumnMetadata(name="BillNo", type="VARCHAR(50)", nullable=False, primary_key=True),
        ColumnMetadata(name="Order_ID", type="INT", nullable=True, foreign_keys=[bill_fk_order]),
        ColumnMetadata(name="Bill_Date", type="DATETIME", nullable=False),
        ColumnMetadata(name="BillAmount", type="DECIMAL(10,2)", nullable=False),
        ColumnMetadata(name="Discount", type="DECIMAL(10,2)", nullable=True),
        ColumnMetadata(name="OtherCharges", type="DECIMAL(10,2)", nullable=True),
        ColumnMetadata(name="Settlement_Type", type="VARCHAR(50)", nullable=True),
    ]
    bill_tbl = TableMetadata(name="Bills", schema_name="dbo", columns=bill_cols, primary_keys=["BillNo"], foreign_keys=[bill_fk_order])

    # 5. OrderLineItems
    line_fk_order = ForeignKeyInfo(constrained_column="Order_ID", target_table="Order", target_column="Order_ID")
    line_cols = [
        ColumnMetadata(name="Line_ID", type="INT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Order_ID", type="INT", nullable=False, foreign_keys=[line_fk_order]),
        ColumnMetadata(name="Description", type="VARCHAR(255)", nullable=False),
        ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False),
        ColumnMetadata(name="Quantity", type="INT", nullable=False),
        ColumnMetadata(name="KOT_Time", type="DATETIME", nullable=True),
    ]
    line_tbl = TableMetadata(name="OrderLineItems", schema_name="dbo", columns=line_cols, primary_keys=["Line_ID"], foreign_keys=[line_fk_order])

    # 6-15 Incidental / Unrelated Tables
    incidental_tables = [
        TableMetadata(name="CashDeclarations", schema_name="dbo", columns=[
            ColumnMetadata(name="CashDeclaration_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Date_Time", type="DATETIME", nullable=False),
            ColumnMetadata(name="Cash", type="DECIMAL(10,2)", nullable=True),
        ]),
        TableMetadata(name="Employees", schema_name="dbo", columns=[
            ColumnMetadata(name="EMP_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="DateOfJoining", type="DATETIME", nullable=True),
            ColumnMetadata(name="EMP_Name", type="VARCHAR(100)", nullable=True),
        ]),
        TableMetadata(name="Customer_Centric_Vouchers", schema_name="dbo", columns=[
            ColumnMetadata(name="Voucher_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Used_Date", type="DATETIME", nullable=True),
            ColumnMetadata(name="Voucher_Code", type="VARCHAR(50)", nullable=True),
        ]),
        TableMetadata(name="Outlet_Centric_Vouchers", schema_name="dbo", columns=[
            ColumnMetadata(name="Voucher_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Date_Added", type="DATETIME", nullable=True),
        ]),
        TableMetadata(name="OptiPro_Sync_Orders", schema_name="dbo", columns=[
            ColumnMetadata(name="Sync_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Date_Time", type="DATETIME", nullable=True),
        ]),
        TableMetadata(name="Order_Pickups", schema_name="dbo", columns=[
            ColumnMetadata(name="Pickup_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Pickup_Time", type="DATETIME", nullable=True),
        ]),
        TableMetadata(name="SMSQueue", schema_name="dbo", columns=[
            ColumnMetadata(name="SMS_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Create_Date", type="DATETIME", nullable=True),
            ColumnMetadata(name="SMS_Text", type="VARCHAR(500)", nullable=True),
        ]),
        TableMetadata(name="Item_Groups", schema_name="dbo", columns=[
            ColumnMetadata(name="Item_Group_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Item_Group", type="VARCHAR(100)", nullable=True),
            ColumnMetadata(name="Group_Category", type="VARCHAR(100)", nullable=True),
        ]),
        TableMetadata(name="Audit_Logs", schema_name="dbo", columns=[
            ColumnMetadata(name="Log_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Date_Time", type="DATETIME", nullable=True),
        ]),
        TableMetadata(name="PrintQueue", schema_name="dbo", columns=[
            ColumnMetadata(name="Print_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Print_Time", type="DATETIME", nullable=True),
        ]),
    ]

    all_tables = [cust_tbl, out_tbl, order_tbl, bill_tbl, line_tbl] + incidental_tables
    return DatabaseSchemaMetadata(database_name="BOPOS_TEST_15", db_type="mssql", tables=all_tables)


def test_classify_table_relevance_rejects_incidental_tables(mock_15_table_schema):
    """
    Ensures classify_table_relevance correctly distinguishes genuine target tables,
    lookup-only master tables, and rejects incidental tables with only generic timestamp matches.
    """
    df_cols = ["restaurant_name", "invoice_no", "date", "customer_name", "item_name", "item_price", "item_quantity"]

    # 1. Customer: genuine (has customer_name, etc.)
    cust_mappings = [
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED", confidence=1.0),
    ]
    role, _ = classify_table_relevance("Customer", cust_mappings, mock_15_table_schema, df_cols)
    assert role == TableRole.TARGET_INSERT

    # 2. Outlets: lookup only
    out_mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED", confidence=0.99),
    ]
    role, _ = classify_table_relevance("Outlets", out_mappings, mock_15_table_schema, df_cols)
    assert role == TableRole.LOOKUP_ONLY

    # 3. Order: genuine target (central transaction table)
    order_mappings = [
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED", confidence=1.0),
    ]
    role, _ = classify_table_relevance("Order", order_mappings, mock_15_table_schema, df_cols)
    assert role == TableRole.TARGET_INSERT

    # 4. Incidental tables with only a single generic date column: must be classified as SKIP
    for inc_tbl, col in [
        ("Employees", "DateOfJoining"),
        ("CashDeclarations", "Date_Time"),
        ("Customer_Centric_Vouchers", "Used_Date"),
        ("Outlet_Centric_Vouchers", "Date_Added"),
        ("OptiPro_Sync_Orders", "Date_Time"),
        ("Order_Pickups", "Pickup_Time"),
        ("SMSQueue", "Create_Date"),
    ]:
        inc_mappings = [
            ColumnMappingResult(excelColumn="date", databaseTable=inc_tbl, databaseColumn=col, status="MATCHED", confidence=0.94, matchType="SEMANTIC"),
        ]
        role, reason = classify_table_relevance(inc_tbl, inc_mappings, mock_15_table_schema, df_cols)
        assert role == TableRole.SKIP, f"Table {inc_tbl} should be SKIP but got {role}"
        assert "Insufficient entity representation" in reason


def test_preview_engine_enforces_minimal_target_tables(mock_15_table_schema):
    """
    Runs MigrationPreviewEngine on a representative DataFrame and verifies:
    - Only genuine target tables are in targetInsertTables
    - Outlets is in lookupOnlyTables
    - Incidental tables are in skippedTables with clear reasons
    """
    df = pd.DataFrame([{
        "restaurant_name": "ANN - BTM",
        "invoice_no": "892170",
        "date": "2026-07-01 12:00:00",
        "customer_name": "Irfan",
        "item_name": "Sweet Lassi",
        "item_price": 59.0,
        "item_quantity": 1,
    }])

    column_mappings = [
        ColumnMappingResult(excelColumn="restaurant_name", databaseTable="Outlets", databaseColumn="Outlet_Name", status="MATCHED", confidence=0.99),
        ColumnMappingResult(excelColumn="invoice_no", databaseTable="Bills", databaseColumn="BillNo", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="date", databaseTable="Bills", databaseColumn="Bill_Date", status="MATCHED", confidence=0.99),
        ColumnMappingResult(excelColumn="date", databaseTable="Order", databaseColumn="Order_DateTime", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="date", databaseTable="CashDeclarations", databaseColumn="Date_Time", status="MATCHED", confidence=0.94),
        ColumnMappingResult(excelColumn="date", databaseTable="Employees", databaseColumn="DateOfJoining", status="MATCHED", confidence=0.94),
        ColumnMappingResult(excelColumn="date", databaseTable="SMSQueue", databaseColumn="Create_Date", status="MATCHED", confidence=0.94),
        ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="item_name", databaseTable="OrderLineItems", databaseColumn="Description", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="item_price", databaseTable="OrderLineItems", databaseColumn="Item_Price", status="MATCHED", confidence=1.0),
        ColumnMappingResult(excelColumn="item_quantity", databaseTable="OrderLineItems", databaseColumn="Quantity", status="MATCHED", confidence=1.0),
    ]

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY, Outlet_Name TEXT, Address TEXT);"))
        conn.execute(text("CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY, Customer_Name TEXT, Mobile_Number TEXT, Address TEXT);"))
        conn.execute(text("CREATE TABLE \"Order\" (Order_ID INTEGER PRIMARY KEY, Customer_ID INTEGER, Outlet_ID INTEGER, Order_DateTime TEXT, Order_Status TEXT, Tax REAL);"))
        conn.execute(text("CREATE TABLE Bills (BillNo TEXT PRIMARY KEY, Order_ID INTEGER, Bill_Date TEXT, BillAmount REAL, Discount REAL, OtherCharges REAL, Settlement_Type TEXT);"))
        conn.execute(text("CREATE TABLE OrderLineItems (Line_ID INTEGER PRIMARY KEY, Order_ID INTEGER, Description TEXT, Item_Price REAL, Quantity INTEGER, KOT_Time TEXT);"))
        conn.execute(text("INSERT INTO Outlets (Outlet_ID, Outlet_Name) VALUES (1, 'ANN - BTM');"))

    preview_res = MigrationPreviewEngine.generate_preview(
        df=df,
        file_name="order_sample.csv",
        schema=mock_15_table_schema,
        column_mappings=column_mappings,
        engine=engine
    )

    planned_ops = preview_res.planned_operations
    assert set(planned_ops.target_insert_tables) == {"Customer", "Order", "Bills", "OrderLineItems"}
    assert "Outlets" in planned_ops.lookup_only_tables
    assert "Outlets" not in planned_ops.target_insert_tables

    # Check that incidental tables were skipped
    for skipped_tbl in ("CashDeclarations", "Employees", "SMSQueue", "Audit_Logs", "PrintQueue"):
        assert skipped_tbl in planned_ops.skipped_tables
        assert planned_ops.table_roles[skipped_tbl] == TableRole.SKIP.value

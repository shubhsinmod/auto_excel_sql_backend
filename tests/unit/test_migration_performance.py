import uuid
import pytest
from unittest.mock import MagicMock, patch
from app.services.migration_service import MigrationService
from app.models.migration import MigrationJobStatus


def test_start_migration_fast_returns_job_without_blocking():
    """Verify that start_migration returns a job ID in milliseconds without blocking on heavy preview computation."""
    service = MigrationService()
    upload_id = f"upload_perf_{uuid.uuid4().hex}"

    with patch("app.services.migration_service.excel_service.get_upload_session") as mock_session, \
         patch("app.services.migration_service.job_manager.run_in_background") as mock_run_bg:

        mock_session.return_value = {
            "filePath": "dummy.xlsx",
            "summary": {"totalRows": 2500}
        }

        job = service.start_migration(upload_id)

        assert isinstance(job, MigrationJobStatus)
        assert job.uploadId == upload_id
        assert job.status in ("PENDING", "PROCESSING", "RUNNING")
        # Background worker was launched
        mock_run_bg.assert_called_once()


def test_collapse_bill_rows_by_invoice_in_excel():
    """Verify that multiple Excel rows with the same invoice number are collapsed to a single header record with summed quantity."""
    from app.migration.engine import collapse_bill_rows_by_invoice

    records = [
        {"InvoiceNo": "INV-001", "Item": "Burger", "Qty": 2},
        {"InvoiceNo": "INV-001", "Item": "Fries", "Qty": 1},
        {"InvoiceNo": "INV-002", "Item": "Coke", "Qty": 3},
    ]
    indices = [0, 1, 2]

    collapsed_records, kept_indices, duplicate_rows, unique_count = collapse_bill_rows_by_invoice(
        records, indices, invoice_column="InvoiceNo", quantity_column="Qty"
    )

    assert unique_count == 2
    assert duplicate_rows == 1
    assert len(collapsed_records) == 2
    assert collapsed_records[0]["InvoiceNo"] == "INV-001"
    assert collapsed_records[0]["Qty"] == 3  # 2 + 1 summed
    assert collapsed_records[1]["InvoiceNo"] == "INV-002"
    assert collapsed_records[1]["Qty"] == 3


def test_item_combo_definations_excluded_from_schema_and_processing():
    """Verify that Item_Combo_Definations is strictly filtered out from schema metadata and preview."""
    from sqlalchemy import create_engine, text
    from app.database.metadata import extract_database_metadata
    from app.migration.preview import classify_table_relevance, TableRole
    from app.models.schema import TableMetadata, ColumnMetadata, DatabaseSchemaMetadata

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Bills (BillNo INTEGER PRIMARY KEY, Bill_Number TEXT)"))
        conn.execute(text("CREATE TABLE Item_Combo_Definations (ComboID INTEGER PRIMARY KEY, ComboName TEXT)"))

    schema = extract_database_metadata(engine, use_cache=False)
    table_names = [t.name.lower() for t in schema.tables]
    assert "item_combo_definations" not in table_names
    assert "bills" in table_names

    # Check preview role classification explicitly skips Item_Combo_Definations
    role, reason = classify_table_relevance("Item_Combo_Definations", [], schema, ["invoice_no"])
    assert role == TableRole.SKIP


def test_duplicate_action_skip_invoices_integration():
    """Verify that existing database invoices are completely skipped when duplicate_action='skip', preserving existing records."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
    from app.models.migration import ColumnMappingResult
    from app.migration.engine import MigrationEngine

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY, Outlet_Name TEXT)"))
        conn.execute(text("INSERT INTO Outlets VALUES (1, 'Main Branch')"))
        conn.execute(text("CREATE TABLE Bills (BillNo INTEGER PRIMARY KEY, Bill_Number TEXT UNIQUE, Total REAL, Outlet_ID INTEGER)"))
        conn.execute(text("CREATE TABLE [Order] (Order_ID INTEGER PRIMARY KEY, BillNo INTEGER, Invoice_No TEXT UNIQUE, Outlet_ID INTEGER)"))
        conn.execute(text("CREATE TABLE OrderLineItems (LineItem_ID INTEGER PRIMARY KEY, Order_ID INTEGER, Item_Name TEXT, Quantity INTEGER)"))
        conn.execute(text("CREATE TABLE Settlement (Settlement_ID INTEGER PRIMARY KEY, BillNo INTEGER, Cash REAL)"))

        # Seed existing invoice INV-001
        conn.execute(text("INSERT INTO Bills VALUES (101, 'INV-001', 50.0, 1)"))
        conn.execute(text("INSERT INTO [Order] VALUES (501, 101, 'INV-001', 1)"))
        conn.execute(text("INSERT INTO OrderLineItems VALUES (1, 501, 'Old Burger', 1)"))
        conn.execute(text("INSERT INTO Settlement VALUES (1, 101, 50.0)"))

    fk_bill = ForeignKeyInfo(constrained_column="BillNo", target_table="Bills", target_column="BillNo", target_schema="dbo")
    fk_order = ForeignKeyInfo(constrained_column="Order_ID", target_table="Order", target_column="Order_ID", target_schema="dbo")
    fk_outlet_bill = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID", target_schema="dbo")
    fk_outlet_order = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Outlets", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Outlet_Name", type="VARCHAR", nullable=False)
                ],
                primary_keys=["Outlet_ID"]
            ),
            TableMetadata(
                name="Bills", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="BillNo", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Bill_Number", type="VARCHAR", nullable=False),
                    ColumnMetadata(name="Total", type="FLOAT", nullable=True),
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, foreign_keys=[fk_outlet_bill])
                ],
                primary_keys=["BillNo"],
                foreign_keys=[fk_outlet_bill]
            ),
            TableMetadata(
                name="Order", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="BillNo", type="INT", nullable=True, foreign_keys=[fk_bill]),
                    ColumnMetadata(name="Invoice_No", type="VARCHAR", nullable=True),
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, foreign_keys=[fk_outlet_order])
                ],
                primary_keys=["Order_ID"],
                foreign_keys=[fk_bill, fk_outlet_order]
            ),
            TableMetadata(
                name="OrderLineItems", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="LineItem_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, foreign_keys=[fk_order]),
                    ColumnMetadata(name="Item_Name", type="VARCHAR", nullable=True),
                    ColumnMetadata(name="Quantity", type="INT", nullable=True)
                ],
                primary_keys=["LineItem_ID"],
                foreign_keys=[fk_order]
            ),
            TableMetadata(
                name="Settlement", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Settlement_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="BillNo", type="INT", nullable=False, foreign_keys=[fk_bill]),
                    ColumnMetadata(name="Cash", type="FLOAT", nullable=True)
                ],
                primary_keys=["Settlement_ID"],
                foreign_keys=[fk_bill]
            ),
        ]
    )

    df = pd.DataFrame([
        # Row 1: Existing invoice INV-001 (should be skipped)
        {"invoice_no": "INV-001", "total": 99.0, "item_name": "New Fries", "item_quantity": 2},
        # Row 2 & 3: New invoice INV-002 with two items
        {"invoice_no": "INV-002", "total": 120.0, "item_name": "Pizza", "item_quantity": 1},
        {"invoice_no": "INV-002", "total": 120.0, "item_name": "Garlic Bread", "item_quantity": 1},
    ])

    mappings = [
        ColumnMappingResult(excelColumn="invoice_no", databaseTable="Bills", databaseColumn="Bill_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="total", databaseTable="Bills", databaseColumn="Total", status="MATCHED"),
        ColumnMappingResult(excelColumn="invoice_no", databaseTable="Order", databaseColumn="Invoice_No", status="MATCHED"),
        ColumnMappingResult(excelColumn="item_name", databaseTable="OrderLineItems", databaseColumn="Item_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="item_quantity", databaseTable="OrderLineItems", databaseColumn="Quantity", status="MATCHED"),
    ]

    upload_id = "upload_test_skip_invoices"
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration(upload_id, duplicate_action="skip")

    assert res.status == "COMPLETED"
    assert res.transaction == "COMMITTED"
    assert res.tables["Bills"].skipped >= 1

    with engine.connect() as conn:
        bills = conn.execute(text("SELECT BillNo, Bill_Number, Total FROM Bills ORDER BY BillNo")).fetchall()
        # Should have exactly 2 bills: original 101, and new 102. INV-001 Total should still be 50.0 (unchanged)
        assert len(bills) == 2
        assert bills[0][1] == "INV-001"
        assert bills[0][2] == 50.0  # unchanged!
        assert bills[1][1] == "INV-002"

        items = conn.execute(text("SELECT LineItem_ID, Order_ID, Item_Name FROM OrderLineItems")).fetchall()
        # Original item preserved, new items inserted, no duplicate for INV-001
        assert len(items) == 3
        item_names = [it[2] for it in items]
        assert "Old Burger" in item_names
        assert "Pizza" in item_names
        assert "Garlic Bread" in item_names
        assert "New Fries" not in item_names


def test_duplicate_action_override_invoices_integration():
    """Verify that existing database invoices are updated in-place with line items cleanly replaced when duplicate_action='override'."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
    from app.models.migration import ColumnMappingResult
    from app.migration.engine import MigrationEngine

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Outlets (Outlet_ID INTEGER PRIMARY KEY, Outlet_Name TEXT)"))
        conn.execute(text("INSERT INTO Outlets VALUES (1, 'Main Branch')"))
        conn.execute(text("CREATE TABLE Bills (BillNo INTEGER PRIMARY KEY, Bill_Number TEXT UNIQUE, Total REAL, Outlet_ID INTEGER)"))
        conn.execute(text("CREATE TABLE [Order] (Order_ID INTEGER PRIMARY KEY, BillNo INTEGER, Invoice_No TEXT UNIQUE, Outlet_ID INTEGER)"))
        conn.execute(text("CREATE TABLE OrderLineItems (LineItem_ID INTEGER PRIMARY KEY, Order_ID INTEGER, Item_Name TEXT, Quantity INTEGER)"))
        conn.execute(text("CREATE TABLE Settlement (Settlement_ID INTEGER PRIMARY KEY, BillNo INTEGER, Cash REAL)"))

        # Seed existing invoice INV-001 with BillNo=101 and old line items
        conn.execute(text("INSERT INTO Bills VALUES (101, 'INV-001', 50.0, 1)"))
        conn.execute(text("INSERT INTO [Order] VALUES (501, 101, 'INV-001', 1)"))
        conn.execute(text("INSERT INTO OrderLineItems VALUES (1, 501, 'Old Burger', 1)"))
        conn.execute(text("INSERT INTO Settlement VALUES (1, 101, 50.0)"))

    fk_bill = ForeignKeyInfo(constrained_column="BillNo", target_table="Bills", target_column="BillNo", target_schema="dbo")
    fk_order = ForeignKeyInfo(constrained_column="Order_ID", target_table="Order", target_column="Order_ID", target_schema="dbo")
    fk_outlet_bill = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID", target_schema="dbo")
    fk_outlet_order = ForeignKeyInfo(constrained_column="Outlet_ID", target_table="Outlets", target_column="Outlet_ID", target_schema="dbo")

    schema_metadata = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Outlets", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Outlet_Name", type="VARCHAR", nullable=False)
                ],
                primary_keys=["Outlet_ID"]
            ),
            TableMetadata(
                name="Bills", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="BillNo", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Bill_Number", type="VARCHAR", nullable=False),
                    ColumnMetadata(name="Total", type="FLOAT", nullable=True),
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, foreign_keys=[fk_outlet_bill])
                ],
                primary_keys=["BillNo"],
                foreign_keys=[fk_outlet_bill]
            ),
            TableMetadata(
                name="Order", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="BillNo", type="INT", nullable=True, foreign_keys=[fk_bill]),
                    ColumnMetadata(name="Invoice_No", type="VARCHAR", nullable=True),
                    ColumnMetadata(name="Outlet_ID", type="INT", nullable=True, foreign_keys=[fk_outlet_order])
                ],
                primary_keys=["Order_ID"],
                foreign_keys=[fk_bill, fk_outlet_order]
            ),
            TableMetadata(
                name="OrderLineItems", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="LineItem_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, foreign_keys=[fk_order]),
                    ColumnMetadata(name="Item_Name", type="VARCHAR", nullable=True),
                    ColumnMetadata(name="Quantity", type="INT", nullable=True)
                ],
                primary_keys=["LineItem_ID"],
                foreign_keys=[fk_order]
            ),
            TableMetadata(
                name="Settlement", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Settlement_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="BillNo", type="INT", nullable=False, foreign_keys=[fk_bill]),
                    ColumnMetadata(name="Cash", type="FLOAT", nullable=True)
                ],
                primary_keys=["Settlement_ID"],
                foreign_keys=[fk_bill]
            ),
        ]
    )

    df = pd.DataFrame([
        # Override INV-001 with new total=200.0 and 2 new line items
        {"invoice_no": "INV-001", "total": 200.0, "item_name": "Premium Steak", "item_quantity": 1},
        {"invoice_no": "INV-001", "total": 200.0, "item_name": "Red Wine", "item_quantity": 1},
    ])

    mappings = [
        ColumnMappingResult(excelColumn="invoice_no", databaseTable="Bills", databaseColumn="Bill_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="total", databaseTable="Bills", databaseColumn="Total", status="MATCHED"),
        ColumnMappingResult(excelColumn="invoice_no", databaseTable="Order", databaseColumn="Invoice_No", status="MATCHED"),
        ColumnMappingResult(excelColumn="item_name", databaseTable="OrderLineItems", databaseColumn="Item_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="item_quantity", databaseTable="OrderLineItems", databaseColumn="Quantity", status="MATCHED"),
    ]

    upload_id = "upload_test_override_invoices"
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration(upload_id, duplicate_action="override")

    assert res.status == "COMPLETED"
    assert res.transaction == "COMMITTED"
    assert res.tables["Bills"].updated == 1

    with engine.connect() as conn:
        bills = conn.execute(text("SELECT BillNo, Bill_Number, Total FROM Bills")).fetchall()
        # BillNo MUST be preserved as 101, and Total updated to 200.0
        assert len(bills) == 1
        assert bills[0][0] == 101
        assert bills[0][1] == "INV-001"
        assert bills[0][2] == 200.0

        orders = conn.execute(text("SELECT Order_ID, BillNo FROM [Order]")).fetchall()
        assert len(orders) == 1
        assert orders[0][0] == 501
        assert orders[0][1] == 101

        items = conn.execute(text("SELECT Item_Name, Quantity FROM OrderLineItems WHERE Order_ID = 501")).fetchall()
        # Old burger was replaced by the 2 new items
        assert len(items) == 2
        item_names = {it[0] for it in items}
        assert item_names == {"Premium Steak", "Red Wine"}


def test_transactional_rollback_on_failure():
    """Verify that any exception during migration execution causes complete rollback with no partial writes."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata
    from app.models.migration import ColumnMappingResult
    from app.migration.engine import MigrationEngine

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE Bills (BillNo INTEGER PRIMARY KEY, Bill_Number TEXT)"))

    schema_metadata = DatabaseSchemaMetadata(
        database_name="TestDB",
        db_type="sqlite",
        tables=[
            TableMetadata(
                name="Bills", schema_name="dbo",
                columns=[
                    ColumnMetadata(name="BillNo", type="INT", nullable=False, primary_key=True),
                    ColumnMetadata(name="Bill_Number", type="VARCHAR", nullable=False)
                ],
                primary_keys=["BillNo"]
            )
        ]
    )

    df = pd.DataFrame([
        {"invoice_no": "INV-VALID-01"},
    ])

    mappings = [
        ColumnMappingResult(excelColumn="invoice_no", databaseTable="Bills", databaseColumn="Bill_Number", status="MATCHED"),
    ]

    upload_id = "upload_test_rollback"
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True), \
         patch.object(engine, "connect", side_effect=RuntimeError("Simulated DB connection failure")):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        with pytest.raises(Exception):
            MigrationEngine.execute_migration(upload_id)

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM Bills")).scalar_one()
        assert count == 0


def test_customer_batch_migration_performance_and_id_propagation():
    """Verify that Customer table migration processes in batch chunks with high performance and exact ID propagation."""
    import time
    from sqlalchemy import create_engine, text
    import pandas as pd
    from app.models.schema import ColumnMetadata, TableMetadata, DatabaseSchemaMetadata, ForeignKeyInfo
    from app.models.migration import ColumnMappingResult
    from app.migration.engine import MigrationEngine

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name VARCHAR(100),
                Mobile_Number VARCHAR(20)
            )
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_Number VARCHAR(50),
                Customer_ID INTEGER
            )
        """))

    customer_tbl = TableMetadata(
        name="Customer",
        schema_name="dbo",
        columns=[
            ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Customer_Name", type="VARCHAR", nullable=False),
            ColumnMetadata(name="Mobile_Number", type="VARCHAR", nullable=True),
        ],
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )
    order_tbl = TableMetadata(
        name="Order",
        schema_name="dbo",
        columns=[
            ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Order_Number", type="VARCHAR", nullable=False),
            ColumnMetadata(name="Customer_ID", type="INT", nullable=True),
        ],
        primary_keys=["Order_ID"],
        foreign_keys=[
            ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")
        ]
    )
    schema_metadata = DatabaseSchemaMetadata(
        database_name="POS_DB",
        db_type="sqlite",
        tables=[customer_tbl, order_tbl]
    )

    # 1000 rows total: 200 distinct customers repeated 5 times each
    rows = []
    for i in range(1000):
        c_num = i % 200
        rows.append({
            "cust_name": f"Customer_{c_num}",
            "mobile": f"980000{c_num:04d}",
            "order_no": f"ORD-{i:04d}"
        })
    df = pd.DataFrame(rows)

    mappings = [
        ColumnMappingResult(excelColumn="cust_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
        ColumnMappingResult(excelColumn="mobile", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="order_no", databaseTable="Order", databaseColumn="Order_Number", status="MATCHED"),
    ]

    upload_id = "upload_perf_customers_1000"
    t0 = time.time()
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema_metadata), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration(upload_id)
    elapsed = time.time() - t0

    assert res.status == "COMPLETED"
    assert elapsed < 3.0  # Must finish fast in batch mode
    cust_stat = res.tables["Customer"]
    assert cust_stat.inserted == 200
    assert cust_stat.reused == 800

    with engine.connect() as conn:
        db_cust_count = conn.execute(text("SELECT COUNT(*) FROM Customer")).scalar()
        assert db_cust_count == 200

        # Verify that all 1000 orders have a non-null Customer_ID assigned
        orders = conn.execute(text("SELECT Order_Number, Customer_ID FROM \"Order\"")).fetchall()
        assert len(orders) == 1000
        for o_num, cid in orders:
            assert cid is not None




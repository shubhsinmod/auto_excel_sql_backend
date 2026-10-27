"""
End-to-end checks of the Customer duplicate rule table through the real migration
engine (SQLite), plus the safety guarantees added for missing-customer fixes:

| Condition                                 | Action                          |
|-------------------------------------------|---------------------------------|
| Name + Mobile both match                  | reuse existing Customer_ID      |
| Name same, Mobile different               | insert new customer             |
| Name same, both Mobiles empty             | reuse existing Customer_ID      |
| Name empty, Mobile available              | match by Mobile                 |
| Name empty, Mobile new                    | insert new customer             |
| Name and Mobile both empty                | do not insert                   |
"""

from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from app.models.migration import ColumnMappingResult
from app.models.schema import ColumnMetadata, DatabaseSchemaMetadata, ForeignKeyInfo, TableMetadata
from app.migration.customer_dedup import CustomerIdentityIndex, preview_customer_counts
from app.migration.engine import MigrationEngine


def _make_db(unique_mobile: bool = False):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE Customer (
                Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Customer_Name VARCHAR(100) NOT NULL,
                Mobile_Number VARCHAR(20) {'UNIQUE' if unique_mobile else ''}
            )
        """))
        conn.execute(text("""
            CREATE TABLE "Order" (
                Order_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Order_Number VARCHAR(50),
                Customer_ID BIGINT,
                Order_Amount FLOAT
            )
        """))
    return engine


@pytest.fixture
def schema():
    customer_tbl = TableMetadata(
        name="Customer", schema_name="dbo",
        columns=[
            ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False),
            ColumnMetadata(name="Mobile_Number", type="VARCHAR", max_length=20, nullable=True),
        ],
        primary_keys=["Customer_ID"], foreign_keys=[],
    )
    order_tbl = TableMetadata(
        name="Order", schema_name="dbo",
        columns=[
            ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Order_Number", type="VARCHAR", max_length=50, nullable=True),
            ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=True),
            ColumnMetadata(name="Order_Amount", type="FLOAT", nullable=True),
        ],
        primary_keys=["Order_ID"],
        foreign_keys=[ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")],
    )
    return DatabaseSchemaMetadata(database_name="POS_TEST", db_type="sqlite", tables=[customer_tbl, order_tbl])


MAPPINGS = [
    ColumnMappingResult(excelColumn="customer_name", databaseTable="Customer", databaseColumn="Customer_Name", status="MATCHED"),
    ColumnMappingResult(excelColumn="customer_phone", databaseTable="Customer", databaseColumn="Mobile_Number", status="MATCHED"),
    ColumnMappingResult(excelColumn="order_no", databaseTable="Order", databaseColumn="Order_Number", status="MATCHED"),
    ColumnMappingResult(excelColumn="amount", databaseTable="Order", databaseColumn="Order_Amount", status="MATCHED"),
]


def _run(engine, schema, df, upload_id):
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=MAPPINGS), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        return MigrationEngine.execute_migration(upload_id)


def _customers(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT Customer_ID, Customer_Name, Mobile_Number FROM Customer ORDER BY Customer_ID")).fetchall()


def _orders(engine):
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text('SELECT Order_Number, Customer_ID FROM "Order"')).fetchall()}


def test_rule_table_single_file(schema):
    engine = _make_db()
    df = pd.DataFrame([
        {"customer_name": "Rahul", "customer_phone": "9876543210", "order_no": "O1", "amount": 1.0},   # new
        {"customer_name": "rahul ", "customer_phone": "98765 43210", "order_no": "O2", "amount": 1.0},  # name+mobile match
        {"customer_name": "Rahul", "customer_phone": "9123456780", "order_no": "O3", "amount": 1.0},   # same name, diff mobile -> new
        {"customer_name": "Amit", "customer_phone": "9876543210", "order_no": "O4", "amount": 1.0},    # diff name, same mobile -> new
        {"customer_name": "Sita", "customer_phone": "", "order_no": "O5", "amount": 1.0},              # name only -> new
        {"customer_name": "SITA", "customer_phone": None, "order_no": "O6", "amount": 1.0},            # same name, both empty -> reuse
        {"customer_name": "", "customer_phone": "9123456780", "order_no": "O7", "amount": 1.0},        # name empty, known mobile -> reuse
        {"customer_name": "NULL", "customer_phone": "9000000001", "order_no": "O8", "amount": 1.0},    # name empty, new mobile -> new
        {"customer_name": "", "customer_phone": "", "order_no": "O9", "amount": 1.0},                  # both empty -> skip
    ])
    res = _run(engine, schema, df, "upload_rule_table")
    assert res.status == "COMPLETED"

    custs = _customers(engine)
    assert [(c[1], c[2]) for c in custs] == [
        ("Rahul", "9876543210"),
        ("Rahul", "9123456780"),
        ("Amit", "9876543210"),
        ("Sita", None),
        ("", "9000000001"),
    ]
    ids = {(c[1], c[2]): c[0] for c in custs}
    orders = _orders(engine)
    assert orders["O1"] == orders["O2"] == ids[("Rahul", "9876543210")]
    assert orders["O3"] == orders["O7"] == ids[("Rahul", "9123456780")]
    assert orders["O4"] == ids[("Amit", "9876543210")]
    assert orders["O5"] == orders["O6"] == ids[("Sita", None)]
    assert orders["O8"] == ids[("", "9000000001")]
    assert orders["O9"] is None

    stat = res.tables["Customer"]
    assert (stat.inserted, stat.reused, stat.skipped) == (5, 3, 1)
    assert stat.verified_inserted == 5

    # Preview/dry-run counts use the same implementation and must agree.
    t_meta = next(t for t in schema.tables if t.name == "Customer")
    assert preview_customer_counts(df, [], MAPPINGS, t_meta) == (5, 1, 3)


def test_rule_table_against_existing_db_customers(schema):
    engine = _make_db()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('Rahul', '9876543210')"))
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('Sita', NULL)"))
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('Sita', '')"))
    before = _customers(engine)

    df = pd.DataFrame([
        {"customer_name": "RAHUL", "customer_phone": "9876543210.0", "order_no": "E1", "amount": 1.0},  # reuse 1
        {"customer_name": "Amit", "customer_phone": "9876543210", "order_no": "E2", "amount": 1.0},     # new
        {"customer_name": "Sita", "customer_phone": "", "order_no": "E3", "amount": 1.0},               # reuse lowest-ID Sita
        {"customer_name": "", "customer_phone": "9876543210", "order_no": "E4", "amount": 1.0},         # mobile -> Rahul (first)
    ])
    res = _run(engine, schema, df, "upload_rule_table_existing")
    assert res.status == "COMPLETED"

    after = _customers(engine)
    assert after[:3] == before  # existing rows never modified
    assert [(c[1], c[2]) for c in after[3:]] == [("Amit", "9876543210")]
    orders = _orders(engine)
    assert orders["E1"] == before[0][0]
    assert orders["E2"] == after[3][0]
    assert orders["E3"] == before[1][0]
    assert orders["E4"] == before[0][0]

    t_meta = next(t for t in schema.tables if t.name == "Customer")
    existing = [{"Customer_ID": c[0], "Customer_Name": c[1], "Mobile_Number": c[2]} for c in before]
    assert preview_customer_counts(df, existing, MAPPINGS, t_meta) == (1, 0, 3)

    # Re-running the same file adds nothing.
    res2 = _run(engine, schema, df, "upload_rule_table_existing_rerun")
    assert res2.tables["Customer"].inserted == 0
    assert _customers(engine) == after


def test_unique_mobile_constraint_conflict_fails_loudly_without_overwrite(schema):
    """A UNIQUE Mobile_Number makes 'different name, same mobile' impossible to store.
    The run must roll back with a clear error - it must not overwrite Rahul or link Amit's order to Rahul."""
    engine = _make_db(unique_mobile=True)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('Rahul', '9876543210')"))
    before = _customers(engine)

    df = pd.DataFrame([
        {"customer_name": "Amit", "customer_phone": "9876543210", "order_no": "U1", "amount": 1.0},
    ])
    with pytest.raises(Exception) as exc_info:
        _run(engine, schema, df, "upload_unique_conflict")
    assert "NEW customer under the duplicate rules" in str(exc_info.value)
    assert _customers(engine) == before
    assert _orders(engine) == {}


def test_prefetch_failure_stops_migration(schema):
    """If existing customers cannot be read, the run must stop instead of inserting duplicates."""
    engine = _make_db()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO Customer (Customer_Name, Mobile_Number) VALUES ('Rahul', '9876543210')"))

    df = pd.DataFrame([{"customer_name": "Rahul", "customer_phone": "9876543210", "order_no": "P1", "amount": 1.0}])
    real_exec = None

    import sqlalchemy.engine.base as base
    real_exec = base.Connection.execute

    def failing_execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM \"Customer\"" in sql and " IN (" in sql and "SELECT" in sql:
            raise RuntimeError("COUNT field incorrect or syntax error")
        return real_exec(self, statement, *args, **kwargs)

    with patch.object(base.Connection, "execute", failing_execute):
        with pytest.raises(Exception) as exc_info:
            _run(engine, schema, df, "upload_prefetch_fail")
    assert "duplicate customers" in str(exc_info.value)
    assert len(_customers(engine)) == 1
    assert _orders(engine) == {}


def test_identity_index_rules_unit():
    idx = CustomerIdentityIndex()
    idx.add("rahul", "9876543210", "A")
    idx.add("", "9123456780", "B")
    idx.add("sita", "", "C")
    assert idx.match("rahul", "9876543210") == "A"
    assert idx.match("amit", "9876543210") is None
    assert idx.match("rahul", "9000000000") is None
    assert idx.match("", "9876543210") == "A"
    assert idx.match("", "9123456780") == "B"
    assert idx.match("sita", "") == "C"
    assert idx.match("rahul", "") is None
    assert idx.match("", "") is None


def test_verification_discrepancy_rolls_back_instead_of_reporting_success(schema):
    """Rows reported as inserted but missing from the destination table must fail the run."""
    engine = _make_db()
    with engine.begin() as conn:
        # Simulate silent loss: every inserted order disappears.
        conn.execute(text(
            'CREATE TRIGGER drop_orders AFTER INSERT ON "Order" '
            'BEGIN DELETE FROM "Order" WHERE Order_ID = NEW.Order_ID; END;'
        ))
    df = pd.DataFrame([{"customer_name": "Rahul", "customer_phone": "9876543210", "order_no": "V1", "amount": 1.0}])
    with pytest.raises(Exception) as exc_info:
        _run(engine, schema, df, "upload_verification_discrepancy")
    assert "verification" in str(exc_info.value).lower()
    assert "Order" in str(exc_info.value)
    # Nothing committed - including the customer written earlier in the same transaction.
    assert _customers(engine) == []


def test_mobile_column_detection_prefers_mapped_mobile_number():
    from app.migration.customer_dedup import detect_customer_mobile_column
    t_meta = TableMetadata(
        name="Customer", schema_name="dbo",
        columns=[
            ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False),
            ColumnMetadata(name="Contact_Number", type="VARCHAR(50)", nullable=True),
            ColumnMetadata(name="Mobile_Number", type="VARCHAR(50)", nullable=True),
        ],
        primary_keys=["Customer_ID"], foreign_keys=[],
    )
    assert detect_customer_mobile_column(t_meta, {"Mobile_Number", "Customer_Name"}) == "Mobile_Number"
    assert detect_customer_mobile_column(t_meta, set()) == "Mobile_Number"
    assert detect_customer_mobile_column(t_meta, {"Contact_Number"}) == "Contact_Number"


def test_existing_customer_found_via_mobile_number_when_contact_number_column_exists():
    """dbo.Customer has Contact_Number before Mobile_Number; existing POS customers that
    only have Mobile_Number filled must still be matched (no duplicate insert)."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE Customer (Customer_ID INTEGER PRIMARY KEY AUTOINCREMENT,
            Customer_Name VARCHAR(100) NOT NULL, Contact_Number VARCHAR(50), Mobile_Number VARCHAR(50))"""))
        conn.execute(text('CREATE TABLE "Order" (Order_ID INTEGER PRIMARY KEY AUTOINCREMENT, Order_Number VARCHAR(50), Customer_ID BIGINT, Order_Amount FLOAT)'))
        conn.execute(text("INSERT INTO Customer (Customer_Name, Contact_Number, Mobile_Number) VALUES ('Rahul', '', '9876543210')"))
    cust = TableMetadata(
        name="Customer", schema_name="dbo",
        columns=[
            ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Customer_Name", type="VARCHAR", max_length=100, nullable=False),
            ColumnMetadata(name="Contact_Number", type="VARCHAR", max_length=50, nullable=True),
            ColumnMetadata(name="Mobile_Number", type="VARCHAR", max_length=50, nullable=True),
        ],
        primary_keys=["Customer_ID"], foreign_keys=[],
    )
    order = TableMetadata(
        name="Order", schema_name="dbo",
        columns=[
            ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
            ColumnMetadata(name="Order_Number", type="VARCHAR", max_length=50, nullable=True),
            ColumnMetadata(name="Customer_ID", type="BIGINT", nullable=True),
            ColumnMetadata(name="Order_Amount", type="FLOAT", nullable=True),
        ],
        primary_keys=["Order_ID"],
        foreign_keys=[ForeignKeyInfo(constrained_column="Customer_ID", target_table="Customer", target_column="Customer_ID")],
    )
    schema = DatabaseSchemaMetadata(database_name="POS_TEST", db_type="sqlite", tables=[cust, order])
    df = pd.DataFrame([
        {"customer_name": "Rahul", "customer_phone": "9876543210", "order_no": "C1", "amount": 1.0},
        {"customer_name": "Neha", "customer_phone": "9000000002", "order_no": "C2", "amount": 1.0},
    ])
    res = _run(engine, schema, df, "upload_contact_vs_mobile")
    assert res.status == "COMPLETED"
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT Customer_ID, Customer_Name, Contact_Number, Mobile_Number FROM Customer ORDER BY Customer_ID")).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == "Rahul"
    # New customers keep the previous storage behaviour (mobile in both columns).
    assert (rows[1][1], rows[1][2], rows[1][3]) == ("Neha", "9000000002", "9000000002")
    orders = _orders(engine)
    assert orders["C1"] == rows[0][0]


def test_multirow_batch_sends_numbers_as_text_for_text_columns():
    """SQL Server multi-row VALUES: a numeric value (cancel reason '14') next to text in a
    VARCHAR column made SQL Server convert the whole column to int (error 245) and roll back."""
    from app.migration.engine import coerce_value_for_column
    text_col = ColumnMetadata(name="Service_Fail_Reason", type="VARCHAR(500)", nullable=True)
    int_col = ColumnMetadata(name="Quantity", type="INT", nullable=True)
    assert coerce_value_for_column(14, text_col) == "14"
    assert coerce_value_for_column(14.0, text_col) == "14"
    assert coerce_value_for_column("Item out of stock", text_col) == "Item out of stock"
    assert coerce_value_for_column(None, text_col) is None
    assert coerce_value_for_column(3, int_col) == 3

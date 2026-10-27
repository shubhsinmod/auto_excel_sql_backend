"""order_type must map to Order.Order_Type as well as Bills.Order_Type."""
from app.mapping.matcher import ColumnMatcher
from app.models.schema import ColumnMetadata, DatabaseSchemaMetadata, TableMetadata


def _schema():
    def tbl(name, cols):
        return TableMetadata(
            name=name, schema_name="dbo",
            columns=[ColumnMetadata(name=f"{name}_PK", type="INT", nullable=False, primary_key=True, identity=True)]
            + [ColumnMetadata(name=c, type=t, nullable=True) for c, t in cols],
            primary_keys=[f"{name}_PK"], foreign_keys=[],
        )
    return DatabaseSchemaMetadata(database_name="POS", db_type="mssql", tables=[
        tbl("Bills", [("BillNo", "BIGINT"), ("Order_Type", "VARCHAR(50)"), ("Settlement_Type", "VARCHAR(50)")]),
        tbl("Order", [("BillNo", "VARCHAR(50)"), ("Order_Type", "VARCHAR(50)"), ("Discount_Type", "VARCHAR(50)")]),
    ])


def test_order_type_maps_to_bills_and_order():
    results = ColumnMatcher.match_columns(["invoice_no", "order_type"], _schema())
    targets = {(r.database_table, r.database_column) for r in results
               if r.excel_column == "order_type" and r.status == "MATCHED"}
    assert targets == {("Bills", "Order_Type"), ("Order", "Order_Type")}

import sys
sys.path.insert(0, ".")
import pandas as pd
from app.migration.engine import get_engine, load_file_dataframe, collapse_bill_rows_by_invoice, sanitize_value, quote_identifier
from app.services.database_service import database_service
from app.mapping.matcher import ColumnMatcher
from sqlalchemy import text

engine = get_engine()
schema = database_service.get_schema_metadata()
schema_tables = {t.name: t for t in schema.tables}
order_meta = schema_tables["Order"]
col_meta_map = {c.name: c for c in order_meta.columns}

df = pd.read_csv("uploads/upload_2fb22f9ee706.csv")
order_invoice_column = "invoice_no"
order_quantity_excel_col = "item_quantity"

entities_records = df.to_dict(orient="records")
entities_df_indices = df.index.tolist()

records, indices, dup_rows, unique_inv = collapse_bill_rows_by_invoice(
    entities_records, entities_df_indices, order_invoice_column, order_quantity_excel_col
)

print(f"Total unique invoice orders: {len(records)}")

# Let's test the first 5 orders (invoices 1057510 to 1057514)
first_orders = records[:5]
mappings = ColumnMatcher.match_columns(df.columns.tolist(), schema)
order_mappings = [m for m in mappings if m.database_table == "Order" and m.status in ("MATCHED", "APPROVED")]
excel_to_db = {m.excel_column: m.database_column for m in order_mappings}

with engine.begin() as conn:
    print("Testing inserting first order...")
    for idx, row in enumerate(first_orders):
        param_dict = {}
        for ex_col, db_col in excel_to_db.items():
            c_meta = col_meta_map.get(db_col)
            c_type = c_meta.type if c_meta else ""
            c_null = c_meta.nullable if c_meta else True
            raw_val = row.get(ex_col)
            param_dict[db_col] = sanitize_value(raw_val, c_type, c_null)
        
        # Add required Order_Outlet_ID (e.g. 1)
        param_dict["Order_Outlet_ID"] = 1
        param_dict["Order_DateTime"] = param_dict.get("Order_DateTime") or "2026-09-03 00:00:26"
        param_dict["BillNo"] = str(row.get("invoice_no"))
        
        insert_cols = [c for c in param_dict.keys() if c in col_meta_map and not getattr(col_meta_map[c], "identity", False)]
        quoted_cols = [quote_identifier(c, "mssql") for c in insert_cols]
        v_params = [f":{c}" for c in insert_cols]
        sql = f"INSERT INTO [Order] ({', '.join(quoted_cols)}) OUTPUT INSERTED.[Order_ID] VALUES ({', '.join(v_params)})"
        
        try:
            res = conn.execute(text(sql), param_dict)
            oid = res.scalar()
            print(f"Row {idx+1} (Invoice {row.get('invoice_no')}) SUCCESS -> Order_ID: {oid}")
        except Exception as e:
            print(f"Row {idx+1} (Invoice {row.get('invoice_no')}) FAILED -> {e}")
            break
    
    # Rollback test
    raise RuntimeError("ROLLBACK TEST ONLY")

# Item_Combo_Definations unresolved item references

The migration resolver does not choose an arbitrary `ItemsMaster.ID` when an item name has multiple matches. It attempts composite disambiguation (such as category/group); if it still cannot identify exactly one record, the FK remains unresolved.

The migration engine inserts SQL `NULL` for an unresolved FK only when the target database column is nullable (or lets a declared DB default apply). If the column is `NOT NULL`, the migration must fail and roll back; Python cannot force a `NULL` into a SQL Server `NOT NULL` column.

To implement the requested “if ID cannot be resolved, save NULL” behavior, review and run `allow_null_item_combo_fks.sql` against the target SQL Server database. It alters only the existing `To_Item_ID` and `From_Item_ID` columns on `dbo.Item_Combo_Definations` to nullable, preserving their integer type. It does not add missing master records or change FK relationships. If your actual schema/table name differs, update the script after inspecting the schema.

After applying the script, rerun Preview/Dry Run first. For an ambiguous `Masala Dosa` match, the resolver should use a unique category/group match if available; otherwise the unresolved FK is written as SQL NULL. Other required non-nullable fields and foreign-key constraints remain enforced, and transaction rollback is preserved.

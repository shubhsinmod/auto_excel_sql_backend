/*
Purpose: Allow unresolved/ambiguous ItemsMaster references to be stored as SQL NULL
in Item_Combo_Definations, as requested. Run this once against the target database
after reviewing the output. This script only changes To_Item_ID and From_Item_ID
from NOT NULL to NULL; it does not modify or delete existing data or FK constraints.

Safety: It only supports built-in integer column types (tinyint, smallint, int, bigint).
It aborts if the table/columns are missing or use another type, rather than guessing.
*/
SET NOCOUNT ON;

DECLARE @schema sysname = N'dbo';
DECLARE @table sysname = N'Item_Combo_Definations';
DECLARE @column sysname;
DECLARE @type sysname;
DECLARE @nullable bit;
DECLARE @sql nvarchar(max);
DECLARE @qualified nvarchar(517) = QUOTENAME(@schema) + N'.' + QUOTENAME(@table);

IF OBJECT_ID(@qualified, N'U') IS NULL
    THROW 51000, 'dbo.Item_Combo_Definations was not found. Update @schema/@table to match the actual schema.', 1;

DECLARE item_fk_columns CURSOR LOCAL FAST_FORWARD FOR
    SELECT v.column_name
    FROM (VALUES (N'To_Item_ID'), (N'From_Item_ID')) AS v(column_name)
    WHERE EXISTS (
        SELECT 1 FROM sys.columns c
        WHERE c.object_id = OBJECT_ID(@qualified) AND c.name = v.column_name
    );

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(@qualified) AND name = N'To_Item_ID')
    THROW 51001, 'To_Item_ID column was not found on dbo.Item_Combo_Definations.', 1;
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(@qualified) AND name = N'From_Item_ID')
    PRINT 'Warning: From_Item_ID was not found; only existing item FK columns will be considered.';

OPEN item_fk_columns;
FETCH NEXT FROM item_fk_columns INTO @column;
WHILE @@FETCH_STATUS = 0
BEGIN
    SELECT @type = TYPE_NAME(c.user_type_id), @nullable = c.is_nullable
    FROM sys.columns c
    WHERE c.object_id = OBJECT_ID(@qualified) AND c.name = @column;

    IF @type NOT IN (N'tinyint', N'smallint', N'int', N'bigint')
    BEGIN
        CLOSE item_fk_columns;
        DEALLOCATE item_fk_columns;
        THROW 51002, 'Item FK column is not a supported integer type. Inspect the schema and alter it manually.', 1;
    END;

    IF @nullable = 0
    BEGIN
        SET @sql = N'ALTER TABLE ' + @qualified + N' ALTER COLUMN ' + QUOTENAME(@column) + N' ' + @type + N' NULL;';
        PRINT @sql;
        EXEC sys.sp_executesql @sql;
    END
    ELSE
        PRINT @column + N' is already nullable; no change required.';

    FETCH NEXT FROM item_fk_columns INTO @column;
END;
CLOSE item_fk_columns;
DEALLOCATE item_fk_columns;

SELECT c.name AS column_name, TYPE_NAME(c.user_type_id) AS data_type, c.is_nullable
FROM sys.columns c
WHERE c.object_id = OBJECT_ID(@qualified)
  AND c.name IN (N'To_Item_ID', N'From_Item_ID');

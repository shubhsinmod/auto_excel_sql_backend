-- ============================================================================
-- SQL Server Migration Script: Excel File Tracking and FileID
-- Database: BOPOS_NAMASTE (or target database)
-- Idempotent: Can be run multiple times safely without duplicate-column errors.
-- ============================================================================

-- 1. Create UploadedFiles table if it does not already exist
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES 
    WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'UploadedFiles'
)
BEGIN
    CREATE TABLE dbo.UploadedFiles (
        FileID INT IDENTITY(1,1) PRIMARY KEY,
        FileName NVARCHAR(255) NOT NULL,
        UploadedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        Status NVARCHAR(30) NOT NULL DEFAULT 'Processing'
    );
    PRINT 'Created table dbo.UploadedFiles.';
END
ELSE
BEGIN
    PRINT 'Table dbo.UploadedFiles already exists.';
END;
GO

-- 2. Add nullable FileID column to destination tables where applicable

-- dbo.Bills
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Bills')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Bills' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Bills ADD FileID INT NULL;
    PRINT 'Added FileID column to dbo.Bills.';
END;

-- dbo.[Order]
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Order')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Order' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.[Order] ADD FileID INT NULL;
    PRINT 'Added FileID column to dbo.[Order].';
END;

-- dbo.OrderLineItems
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'OrderLineItems')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'OrderLineItems' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.OrderLineItems ADD FileID INT NULL;
    PRINT 'Added FileID column to dbo.OrderLineItems.';
END;

-- dbo.Customer
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Customer')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Customer' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Customer ADD FileID INT NULL;
    PRINT 'Added FileID column to dbo.Customer.';
END;

-- dbo.Settlements
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlements')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlements' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Settlements ADD FileID INT NULL;
    PRINT 'Added FileID column to dbo.Settlements.';
END;

-- dbo.Settlement (singular table name fallback if present)
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlement')
   AND NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlement' AND COLUMN_NAME = 'FileID')
BEGIN
    ALTER TABLE dbo.Settlement ADD FileID INT NULL;
    PRINT 'Added FileID column to dbo.Settlement.';
END;
GO

-- 3. Add Foreign Key constraints referencing dbo.UploadedFiles(FileID)

-- FK on Bills
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Bills')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Bills_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Bills ADD CONSTRAINT FK_Bills_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
    PRINT 'Created constraint FK_Bills_UploadedFiles.';
END;

-- FK on Order
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Order')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Order_UploadedFiles')
BEGIN
    ALTER TABLE dbo.[Order] ADD CONSTRAINT FK_Order_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
    PRINT 'Created constraint FK_Order_UploadedFiles.';
END;

-- FK on OrderLineItems
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'OrderLineItems')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_OrderLineItems_UploadedFiles')
BEGIN
    ALTER TABLE dbo.OrderLineItems ADD CONSTRAINT FK_OrderLineItems_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
    PRINT 'Created constraint FK_OrderLineItems_UploadedFiles.';
END;

-- FK on Customer
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Customer')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Customer_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Customer ADD CONSTRAINT FK_Customer_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
    PRINT 'Created constraint FK_Customer_UploadedFiles.';
END;

-- FK on Settlements
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlements')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Settlements_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Settlements ADD CONSTRAINT FK_Settlements_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
    PRINT 'Created constraint FK_Settlements_UploadedFiles.';
END;

-- FK on Settlement (singular)
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Settlement')
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_Settlement_UploadedFiles')
BEGIN
    ALTER TABLE dbo.Settlement ADD CONSTRAINT FK_Settlement_UploadedFiles
    FOREIGN KEY (FileID) REFERENCES dbo.UploadedFiles(FileID);
    PRINT 'Created constraint FK_Settlement_UploadedFiles.';
END;
GO

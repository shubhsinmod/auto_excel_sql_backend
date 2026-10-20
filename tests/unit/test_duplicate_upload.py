import io
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app
from app.services.excel_service import ExcelService

client = TestClient(app)


def test_duplicate_upload_allowed():
    """Verify that uploading an identical file is allowed when duplicate check is disabled."""
    file_bytes = b"colA,colB\n1,2\n"
    file_content = io.BytesIO(file_bytes)

    with patch("app.excel.validator.validate_excel_workbook", return_value=(True, None)), \
         patch("app.excel.analyzer.analyze_excel_file") as mock_analyze:

        mock_analyze.return_value = {
            "uploadId": "upload_dup_test",
            "fileName": "test.csv",
            "fileSize": len(file_bytes),
            "sheets": ["Sheet1"],
            "summary": {"totalRows": 1, "totalColumns": 2},
            "columns": [],
            "sampleRows": [],
            "suggestedMappings": [],
            "validationRules": []
        }

        response = client.post(
            "/api/excel/upload",
            files={"file": ("test.csv", file_content, "text/csv")}
        )

        assert response.status_code == 200
        assert response.json()["uploadId"].startswith("upload_")


def test_unique_upload_allowed():
    """Verify that uploading a new file proceeds normally."""
    file_bytes = b"colA,colB\n1,2\n"
    file_content = io.BytesIO(file_bytes)

    with patch("app.services.excel_service.ExcelService.find_successful_import", return_value=None), \
         patch("app.excel.validator.validate_excel_workbook", return_value=(True, None)), \
         patch("app.excel.analyzer.analyze_excel_file") as mock_analyze:

        mock_analyze.return_value = {
            "uploadId": "upload_new_test",
            "fileName": "test.csv",
            "fileSize": len(file_bytes),
            "sheets": ["Sheet1"],
            "summary": {"totalRows": 1, "totalColumns": 2},
            "columns": [],
            "sampleRows": [],
            "suggestedMappings": [],
            "validationRules": []
        }

        response = client.post(
            "/api/excel/upload",
            files={"file": ("test.csv", file_content, "text/csv")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["uploadId"].startswith("upload_")
        assert data["fileName"] == "test.csv"


def test_bills_identity_insert_mssql():
    """Verify that inserting into Bills table on MSSQL executes SET IDENTITY_INSERT [Bills] ON and OFF,
    and inserts explicit BillNo invoice number."""
    from app.migration.engine import MigrationEngine
    from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
    from app.models.migration import ColumnMappingResult
    import pandas as pd
    from unittest.mock import MagicMock, patch

    bills_cols = [
        ColumnMetadata(name="BillNo", type="BIGINT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Bill_Number", type="VARCHAR(50)", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="BillAmount", type="DECIMAL(10,2)", nullable=True, primary_key=False, identity=False),
    ]
    schema = DatabaseSchemaMetadata(
        database_name="BOPOS_Test",
        db_type="mssql",
        tables=[
            TableMetadata(name="Bills", schema_name="dbo", columns=bills_cols, primary_keys=["BillNo"]),
        ]
    )

    df = pd.DataFrame([{
        "Invoice No": 1068951,
        "Amount": 150.0
    }])

    mappings = [
        ColumnMappingResult(excelColumn="Invoice No", databaseTable="Bills", databaseColumn="Bill_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="Amount", databaseTable="Bills", databaseColumn="BillAmount", status="MATCHED"),
    ]

    mock_conn = MagicMock()
    mock_conn.dialect.name = "mssql"
    executed_statements = []
    executed_params = []

    def fake_execute(stmt, params=None):
        sql_str = str(stmt)
        executed_statements.append(sql_str)
        executed_params.append(params)
        res = MagicMock()
        res.scalar.return_value = 1068951
        res.rowcount = 1
        res.fetchall.return_value = []
        return res

    mock_conn.execute.side_effect = fake_execute

    mock_engine = MagicMock()
    mock_engine.name = "mssql"
    mock_engine.dialect.name = "mssql"
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    upload_id = "test_bills_ident_upload_1"
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=mock_engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration(
            upload_id=upload_id,
            duplicate_action="override"
        )

    assert res.total_inserted == 1
    # Check that SET IDENTITY_INSERT was called
    identity_on_called = any("SET IDENTITY_INSERT [Bills] ON" in s for s in executed_statements)
    identity_off_called = any("SET IDENTITY_INSERT [Bills] OFF" in s for s in executed_statements)
    assert identity_on_called, f"Expected SET IDENTITY_INSERT [Bills] ON in {executed_statements}"
    assert identity_off_called, f"Expected SET IDENTITY_INSERT [Bills] OFF in {executed_statements}"

    # Check that INSERT INTO [Bills] contained [BillNo]
    insert_stmts = [s for s in executed_statements if "INSERT INTO [Bills]" in s]
    assert len(insert_stmts) == 1
    assert "[BillNo]" in insert_stmts[0]

    # Check that param dict contains BillNo == 1068951
    insert_idx = executed_statements.index(insert_stmts[0])
    params = executed_params[insert_idx]
    assert params["BillNo"] == 1068951


def test_bills_duplicate_override_mssql():
    """Verify that when a duplicate bill exists and duplicate_action='override',
    an UPDATE statement is executed without BillNo in the SET clause."""
    from app.migration.engine import MigrationEngine
    from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
    from app.models.migration import ColumnMappingResult
    import pandas as pd
    from unittest.mock import MagicMock, patch

    bills_cols = [
        ColumnMetadata(name="BillNo", type="BIGINT", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Bill_Number", type="VARCHAR(50)", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="BillAmount", type="DECIMAL(10,2)", nullable=True, primary_key=False, identity=False),
    ]
    schema = DatabaseSchemaMetadata(
        database_name="BOPOS_Test",
        db_type="mssql",
        tables=[
            TableMetadata(name="Bills", schema_name="dbo", columns=bills_cols, primary_keys=["BillNo"]),
        ]
    )

    df = pd.DataFrame([{
        "Invoice No": 1057510,
        "Amount": 282.0
    }])

    mappings = [
        ColumnMappingResult(excelColumn="Invoice No", databaseTable="Bills", databaseColumn="Bill_Number", status="MATCHED"),
        ColumnMappingResult(excelColumn="Amount", databaseTable="Bills", databaseColumn="BillAmount", status="MATCHED"),
    ]

    mock_conn = MagicMock()
    mock_conn.dialect.name = "mssql"
    executed_statements = []
    executed_params = []

    def fake_execute(stmt, params=None):
        sql_str = str(stmt)
        executed_statements.append(sql_str)
        executed_params.append(params)
        res = MagicMock()
        if "INSERT INTO [Bills]" in sql_str:
            # Simulate SQL Server duplicate PK error on explicit BillNo insert
            raise Exception("Violation of PRIMARY KEY constraint 'PK_Bills'. Cannot insert duplicate key in object 'dbo.Bills'. The duplicate key value is (1057510).")
        res.mappings.return_value.all.return_value = []
        res.fetchall.return_value = []
        res.rowcount = 1
        res.scalar.return_value = 1
        return res

    mock_conn.execute.side_effect = fake_execute

    mock_engine = MagicMock()
    mock_engine.name = "mssql"
    mock_engine.dialect.name = "mssql"
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    upload_id = "test_bills_override_upload_2"
    with patch("app.services.excel_service.excel_service.get_upload_session") as mock_session, \
         patch("app.migration.engine.get_engine", return_value=mock_engine), \
         patch("app.services.database_service.database_service.get_schema_metadata", return_value=schema), \
         patch("app.migration.engine.load_file_dataframe", return_value=df), \
         patch("app.migration.engine.pd.io.common.file_exists", return_value=True), \
         patch("app.mapping.matcher.ColumnMatcher.match_columns", return_value=mappings), \
         patch("os.path.exists", return_value=True):
        mock_session.return_value = {"filePath": "dummy.xlsx", "sheetName": None, "headerRowNumber": 1}
        res = MigrationEngine.execute_migration(
            upload_id=upload_id,
            duplicate_action="override"
        )

    assert res.total_updated == 1
    # Verify that UPDATE was called
    update_stmts = [s for s in executed_statements if "UPDATE [Bills] SET" in s]
    assert len(update_stmts) == 1
    # Verify [BillNo] = is NOT in the SET clause (only in WHERE clause)
    set_part = update_stmts[0].split("WHERE")[0]
    assert "[BillNo] =" not in set_part, f"BillNo should not be in SET clause: {set_part}"
    assert "WHERE [BillNo] = :_override_bno" in update_stmts[0]


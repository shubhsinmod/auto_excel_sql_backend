from fastapi.testclient import TestClient
from unittest.mock import patch
from main import app

client = TestClient(app)


def test_read_root():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "Intelligent Excel-to-SQL" in data["app"]


def test_api_database_test_endpoint():
    with patch("app.services.database_service.database_service.test_connection") as mock_conn:
        mock_conn.return_value = {
            "connected": True,
            "database": "SampleDB",
            "server": "localhost",
            "version": "SQL Server 2022",
            "message": "Connected",
            "error": None,
        }
        response = client.get("/api/database/test")
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["database"] == "SampleDB"


def test_api_database_connect_endpoint():
    with patch("app.services.database_service.database_service.test_connection") as mock_conn:
        mock_conn.return_value = {
            "connected": True,
            "database": "MyTargetDB",
            "server": "sql.example.com",
            "version": "SQL Server 2019",
            "message": "Connected",
            "error": None,
        }
        payload = {
            "server": "sql.example.com",
            "port": 1433,
            "database": "MyTargetDB",
            "username": "sa",
            "password": "Password123",
            "driver": "ODBC Driver 17 for SQL Server",
            "trusted_connection": False,
        }
        response = client.post("/api/database/connect", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["database"] == "MyTargetDB"


def test_api_migration_process_invalid_upload_returns_400():
    """Verify that requesting process on a nonexistent upload ID returns 400 Bad Request."""
    response = client.post("/api/migration/process/upload_nonexistent_xyz")
    assert response.status_code == 400
    assert "not found or expired" in response.json()["detail"].lower()


def test_api_migration_process_success_contract():
    """Verify that process endpoint returns complete Step3ProcessingResponse contract."""
    with patch("app.services.migration_service.migration_service.process_step3") as mock_step3:
        from app.models.migration import Step3ProcessingResponse, ValidationSummary, PlannedOperationsSummary
        from app.models.schema import DatabaseSchemaMetadata

        mock_step3.return_value = Step3ProcessingResponse(
            uploadId="upload_test_contract",
            status="COMPLETED",
            schema=DatabaseSchemaMetadata(database_name="TestDB", db_type="mssql", tables=[]),
            columnMappings=[],
            valueResolutions=[],
            tablesProcessed=["Order", "Customer"],
            matchedColumnsCount=2,
            matchedValuesCount=10,
            unmatchedValuesCount=0,
            ambiguousValuesCount=0,
            processingTimeSeconds=0.123,
            previewRows=[],
            dependencyPlan=[],
            validationSummary=ValidationSummary(
                totalRows=10,
                validRows=8,
                invalidRows=2,
                warningsCount=0,
                matchedColumnsCount=2,
                unmatchedColumnsCount=0,
                unresolvedForeignKeysCount=0,
                errors=[],
                warnings=[],
                circularDependencies=[],
                canExecute=False
            ),
            plannedOperations=PlannedOperationsSummary(
                totalPlannedInserts=10,
                totalPlannedUpdates=0,
                totalPlannedSkips=0,
                plannedInsertsByTable={"Order": 10},
                plannedUpdatesByTable={},
                plannedSkipsByTable={},
                statement="No writes performed",
                canExecute=False
            ),
            executionOrder=["Customer", "Order"],
            statement="No database writes occurred.",
            canExecute=False
        )

        response = client.post("/api/migration/process/upload_test_contract")
        assert response.status_code == 200
        data = response.json()
        assert data["uploadId"] == "upload_test_contract"
        assert data["status"] == "COMPLETED"
        assert data["canExecute"] is False
        assert data["tablesProcessed"] == ["Order", "Customer"]
        assert data["validationSummary"]["invalidRows"] == 2


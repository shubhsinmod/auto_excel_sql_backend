from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_migration_history_endpoint():
    response = client.get("/api/migration/history")
    assert response.status_code == 200
    data = response.json()
    assert "totalFiles" in data
    assert "completedCount" in data
    assert "failedCount" in data
    assert "processingCount" in data
    assert "totalRowsMigrated" in data
    assert "files" in data
    assert isinstance(data["files"], list)
    assert data["totalFiles"] >= len(data["files"])

def test_excel_history_endpoint():
    response = client.get("/api/excel/history")
    assert response.status_code == 200
    data = response.json()
    assert "totalFiles" in data
    assert "files" in data

def test_migration_files_list_endpoint():
    response = client.get("/api/migration/files")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    if len(data) > 0:
        first = data[0]
        assert "FileID" in first
        assert "FileName" in first
        assert "Status" in first

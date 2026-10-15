from unittest.mock import patch, MagicMock
from app.database.connection import db_manager
from app.services.database_service import database_service
from app.models.schema import DatabaseConnectRequest


def test_database_manager_test_connection_failure():
    """Test connection handling when database is unavailable."""
    with patch.object(db_manager, "create_custom_engine") as mock_engine:
        mock_engine.side_effect = Exception("Login failed for user 'sa'")
        result = db_manager.test_connection(
            server="localhost",
            database="NonExistentDB",
            username="sa",
            password="secretpassword",
        )
        assert result["connected"] is False
        assert "Login failed" in result["error"]
        assert "secretpassword" not in result["error"]


def test_database_service_returns_pydantic_response():
    """Test database service response format."""
    with patch.object(db_manager, "test_connection") as mock_test:
        mock_test.return_value = {
            "connected": True,
            "database": "TestDB",
            "server": "localhost",
            "version": "Microsoft SQL Server 2022",
            "message": "Connected successfully",
        }
        req = DatabaseConnectRequest(
            server="localhost",
            database="TestDB",
            username="sa",
            password="pass",
        )
        res = database_service.test_connection(req)
        assert res.connected is True
        assert res.database == "TestDB"
        assert res.server == "localhost"

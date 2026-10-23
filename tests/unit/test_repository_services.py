import pytest
import hashlib
from unittest.mock import MagicMock

from app.repositories.user_repository import UserRepository, DuplicateUserError
from app.repositories.file_tracking_repository import FileTrackingRepository
from app.repositories.import_history_repository import ImportHistoryRepository
from app.services.auth_service import AuthService, AuthenticationError, AuthUnavailableError, verify_password
from app.services.user_service import UserService
from app.services.history_service import HistoryService


# ---------------------------------------------------------------------------
# UserRepository Tests
# ---------------------------------------------------------------------------

class MockConnection:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.last_query = str(query)
        self.last_params = params
        mock_res = MagicMock()
        mock_res.mappings.return_value.all.return_value = self.rows
        return mock_res


class MockEngine:
    def __init__(self, rows, name="mssql"):
        self.name = name
        self.conn = MockConnection(rows)

    def connect(self):
        return self.conn

    def begin(self):
        return self.conn


def test_user_repository_finds_single_user():
    pwd_hash = hashlib.sha1(b"pass123").hexdigest()
    engine = MockEngine([{"UserName": "admin", "Password": pwd_hash}], name="mssql")
    repo = UserRepository(engine_provider=lambda: engine)

    user = repo.get_user_by_username("admin")
    assert user is not None
    assert user["UserName"] == "admin"
    assert user["Password"] == pwd_hash
    assert "TOP 2" in engine.conn.last_query


def test_user_repository_raises_on_duplicate_user():
    pwd_hash = hashlib.sha1(b"pass123").hexdigest()
    rows = [
        {"UserName": "admin", "Password": pwd_hash},
        {"UserName": "admin", "Password": pwd_hash}
    ]
    engine = MockEngine(rows, name="mssql")
    repo = UserRepository(engine_provider=lambda: engine)

    with pytest.raises(DuplicateUserError):
        repo.get_user_by_username("admin")


def test_user_repository_returns_none_when_user_not_found():
    engine = MockEngine([], name="postgresql")
    repo = UserRepository(engine_provider=lambda: engine)

    user = repo.get_user_by_username("nonexistent")
    assert user is None
    assert "LIMIT 2" in engine.conn.last_query


# ---------------------------------------------------------------------------
# AuthService Tests
# ---------------------------------------------------------------------------

def test_auth_service_successful_login():
    pwd_hash = hashlib.sha1(b"correct_pass").hexdigest()
    mock_repo = MagicMock(spec=UserRepository)
    mock_repo.find_users_by_username.return_value = [{"UserName": "john_doe", "Password": pwd_hash}]

    service = AuthService(user_repo=mock_repo)
    response = service.authenticate_user("john_doe", "correct_pass")

    assert response.authenticated is True
    assert response.username == "john_doe"


def test_auth_service_rejects_wrong_password():
    pwd_hash = hashlib.sha1(b"correct_pass").hexdigest()
    mock_repo = MagicMock(spec=UserRepository)
    mock_repo.find_users_by_username.return_value = [{"UserName": "john_doe", "Password": pwd_hash}]

    service = AuthService(user_repo=mock_repo)
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        service.authenticate_user("john_doe", "wrong_pass")


def test_auth_service_handles_database_error():
    mock_repo = MagicMock(spec=UserRepository)
    mock_repo.find_users_by_username.side_effect = RuntimeError("DB connection dropped")

    service = AuthService(user_repo=mock_repo)
    with pytest.raises(AuthUnavailableError):
        service.authenticate_user("john_doe", "pass")


# ---------------------------------------------------------------------------
# UserService Tests
# ---------------------------------------------------------------------------

def test_user_service_get_profile():
    mock_repo = MagicMock(spec=UserRepository)
    mock_repo.get_user_by_username.return_value = {"UserName": "alice", "Password": "secret_hash"}

    service = UserService(user_repo=mock_repo)
    profile = service.get_user_profile("alice")

    assert profile is not None
    assert profile.username == "alice"
    assert profile.is_active is True


def test_user_service_user_exists():
    mock_repo = MagicMock(spec=UserRepository)
    mock_repo.find_users_by_username.return_value = [{"UserName": "bob"}]

    service = UserService(user_repo=mock_repo)
    assert service.user_exists("bob") is True

    mock_repo.find_users_by_username.return_value = []
    assert service.user_exists("unknown") is False


# ---------------------------------------------------------------------------
# ImportHistoryRepository Tests (Isolated SQLite database in tmp)
# ---------------------------------------------------------------------------

def test_import_history_repository_record_and_find(tmp_path):
    db_file = str(tmp_path / "test_import_history.sqlite3")
    repo = ImportHistoryRepository(db_path=db_file)

    test_sha = "abc123def4567890123456789012345678901234567890123456789012345678"
    assert repo.find_by_sha256(test_sha) is None

    repo.record_import(test_sha, "sales.xlsx", "upload_test_123")
    found = repo.find_by_sha256(test_sha)

    assert found is not None
    assert found["original_filename"] == "sales.xlsx"
    assert found["upload_id"] == "upload_test_123"

    items = repo.list_successful_imports(limit=10)
    assert len(items) == 1
    assert items[0]["upload_id"] == "upload_test_123"


# ---------------------------------------------------------------------------
# HistoryService Tests
# ---------------------------------------------------------------------------

def test_history_service_aggregates_metrics():
    mock_tracking_repo = MagicMock(spec=FileTrackingRepository)
    mock_tracking_repo.list_files.return_value = [
        {"FileID": 101, "FileName": "orders.xlsx", "UploadedAt": "2026-10-23T10:00:00", "Status": "Completed"},
        {"FileID": 102, "FileName": "items.csv", "UploadedAt": "2026-10-23T10:05:00", "Status": "Processing"}
    ]
    mock_tracking_repo.get_total_count.return_value = 2

    mock_import_repo = MagicMock(spec=ImportHistoryRepository)
    mock_import_repo.list_successful_imports.return_value = []

    service = HistoryService(
        tracking_repo=mock_tracking_repo,
        import_history_repo=mock_import_repo
    )

    summary = service.get_history_summary(limit=10)
    assert summary["totalFiles"] >= 2
    assert summary["completedCount"] >= 1
    assert summary["processingCount"] >= 1

    uploaded_files = service.get_uploaded_files(limit=10)
    assert len(uploaded_files) >= 2
    assert uploaded_files[0]["FileID"] in (101, 102)

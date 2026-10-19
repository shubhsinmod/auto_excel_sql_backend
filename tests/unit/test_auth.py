import base64
import hashlib

import pytest
from fastapi import HTTPException

from app.api import auth
from app.api.auth import LoginRequest, login, verify_password


def django_pbkdf2_hash(password: str, salt: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    encoded = base64.b64encode(digest).decode("ascii").strip()
    return f"pbkdf2_sha256${iterations}${salt}${encoded}"


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.parameters = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, parameters):
        self.query = str(query)
        self.parameters = parameters
        return FakeResult(self.rows)


class FakeEngine:
    def __init__(self, rows, name: str = "mssql"):
        self.connection = FakeConnection(rows)
        self.name = name

    def connect(self):
        return self.connection


def test_verify_legacy_sha1_hash():
    stored_hash = hashlib.sha1(b"secret").hexdigest()

    assert verify_password("secret", stored_hash)
    assert not verify_password("wrong", stored_hash)


def test_verify_django_pbkdf2_sha256_hash():
    stored_hash = django_pbkdf2_hash("secret", "test-salt", 12000)

    assert verify_password("secret", stored_hash)
    assert not verify_password("wrong", stored_hash)


@pytest.mark.parametrize("stored_hash", ["", "not-a-supported-hash", "pbkdf2_sha256$0$salt$hash"])
def test_rejects_empty_unknown_or_invalid_hash(stored_hash):
    assert not verify_password("secret", stored_hash)


def test_login_reads_user_by_parameterized_username_and_returns_no_hash(monkeypatch):
    password_hash = hashlib.sha1(b"secret").hexdigest()
    engine = FakeEngine([{"UserName": "operator", "Password": password_hash}])
    monkeypatch.setattr(auth, "get_engine", lambda: engine)

    result = login(LoginRequest(username=" operator ", password="secret"))

    assert result.authenticated is True
    assert result.username == "operator"
    assert engine.connection.parameters == {"username": "operator"}
    assert "[Password]" in engine.connection.query
    assert "secret" not in engine.connection.query
    assert "INSERT" not in engine.connection.query.upper()
    assert "UPDATE" not in engine.connection.query.upper()
    assert "DELETE" not in engine.connection.query.upper()


def test_login_rejects_invalid_password(monkeypatch):
    password_hash = hashlib.sha1(b"secret").hexdigest()
    monkeypatch.setattr(auth, "get_engine", lambda: FakeEngine([{"UserName": "operator", "Password": password_hash}]))

    with pytest.raises(HTTPException) as error:
        login(LoginRequest(username="operator", password="wrong"))

    assert error.value.status_code == 401
    assert error.value.detail == "Invalid username or password"


def test_login_rejects_duplicate_username_matches(monkeypatch):
    rows = [
        {"UserName": "operator", "Password": hashlib.sha1(b"secret").hexdigest()},
        {"UserName": "operator", "Password": hashlib.sha1(b"secret").hexdigest()}
    ]
    monkeypatch.setattr(auth, "get_engine", lambda: FakeEngine(rows))

    with pytest.raises(HTTPException) as error:
        login(LoginRequest(username="operator", password="secret"))

    assert error.value.status_code == 401


def test_login_supports_postgresql_dialect_with_limit(monkeypatch):
    password_hash = hashlib.sha1(b"secret").hexdigest()
    # In PostgreSQL, columns and tables might be lowercase
    engine = FakeEngine([{"username": "operator", "password": password_hash}], name="postgresql")
    monkeypatch.setattr(auth, "get_engine", lambda: engine)

    result = login(LoginRequest(username=" operator ", password="secret"))

    assert result.authenticated is True
    assert result.username == "operator"
    assert engine.connection.parameters == {"username": "operator"}
    assert "LIMIT 2" in engine.connection.query
    assert "TOP" not in engine.connection.query
    assert "[dbo]" not in engine.connection.query
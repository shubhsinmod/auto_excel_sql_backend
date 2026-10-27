"""SqlServerTransactionGuard: stop when SQL Server silently rolled back the migration transaction."""
from unittest.mock import MagicMock

import pytest

from app.migration.transaction import SqlServerTransactionGuard


class FakeCursor:
    def __init__(self, trancount, xact_state, tx_id=None):
        self.state = (trancount, xact_state, tx_id)
        self.executed = []

    def execute(self, sql, *args):
        self.executed.append(sql)

    def fetchone(self):
        return self.state


def _guard():
    return SqlServerTransactionGuard(MagicMock(), "upload_x")


def test_rolled_back_transaction_after_write_raises_with_original_error():
    g = _guard()
    g._before(None, FakeCursor(1, 1), "INSERT INTO [Customer] VALUES (?)", (), None, False)
    g._after(None, None, "INSERT INTO [Customer] VALUES (?)", (), None, False)
    # multi-row insert fails (no after event) - code swallows it and continues
    g._before(None, FakeCursor(1, 1), "INSERT INTO [Order] VALUES (?), (?)", (), None, False)
    g.pending_error = "Conversion failed when converting the nvarchar value 'abc' to data type int"
    with pytest.raises(RuntimeError) as exc:
        g._before(None, FakeCursor(0, 0), "INSERT INTO [Order] VALUES (?)", (), None, False)
    assert "rolled back the migration transaction" in str(exc.value)
    assert "Conversion failed" in str(exc.value)


def test_doomed_transaction_raises():
    g = _guard()
    g._before(None, FakeCursor(1, 1), "SELECT 1", (), None, False)  # never completes -> failed
    with pytest.raises(RuntimeError):
        g._before(None, FakeCursor(1, -1), "SELECT 2", (), None, False)


def test_statement_level_error_with_live_transaction_continues():
    g = _guard()
    g._before(None, FakeCursor(1, 1), "INSERT INTO [Bills] VALUES (?)", (), None, False)
    g._after(None, None, "INSERT INTO [Bills] VALUES (?)", (), None, False)
    g.pending_error = "Violation of PRIMARY KEY constraint"
    cur = FakeCursor(1, 1)
    g._before(None, cur, "UPDATE [Bills] SET x = ?", (), None, False)  # no raise
    assert len(cur.executed) == 1 and cur.executed[0].startswith("SELECT @@TRANCOUNT, XACT_STATE()")
    assert g.pending_error is None


def test_error_before_any_write_is_not_treated_as_lost_data():
    g = _guard()
    g.pending_error = "Invalid object name 'Bills'"
    g._before(None, FakeCursor(0, 0), "SELECT * FROM [Customer]", (), None, False)  # no raise


def test_new_transaction_after_silent_rollback_is_detected_by_transaction_id():
    """ODBC opens a new transaction after SQL Server's rollback: @@TRANCOUNT is 1 again, the id differs."""
    g = _guard()
    g._before(None, FakeCursor(1, 1, 500), "INSERT INTO [Customer] VALUES (?)", (), None, False)
    g._after(None, None, "INSERT INTO [Customer] VALUES (?)", (), None, False)
    g._before(None, FakeCursor(1, 1, 500), "INSERT INTO [Order] VALUES (?), (?)", (), None, False)  # records tx 500, then fails
    assert g.tx_id == 500
    g.pending_error = "Conversion failed when converting the nvarchar value 'x' to data type int. (245)"
    with pytest.raises(RuntimeError) as exc:
        g._before(None, FakeCursor(1, 1, 777), "INSERT INTO [Order] VALUES (?)", (), None, False)
    assert "245" in str(exc.value)

"""
BillRepository implementation using SQLAlchemy ORM.

Centralizes database operations for dbo.Bills.
"""

from typing import Optional, List, Dict, Any
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.models.bill import Bill
from app.database.session import get_session_context
from app.core.logging import logger


class BillRepository:
    """
    Repository layer centralizing all database operations for dbo.Bills.
    """

    def __init__(self, session: Optional[Session] = None):
        self._session = session

    def get_by_bill_no(self, bill_no: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single bill by BillNo."""
        if self._session is not None:
            b = self._session.get(Bill, bill_no)
            return b.to_dict() if b else None
        with get_session_context() as session:
            b = session.get(Bill, bill_no)
            return b.to_dict() if b else None

    def find_by_bill_number(self, bill_number: str) -> Optional[Dict[str, Any]]:
        """Finds a bill by alphanumeric Bill_Number string."""
        stmt = select(Bill).where(Bill.Bill_Number == bill_number).limit(1)
        if self._session is not None:
            b = self._session.scalars(stmt).first()
            return b.to_dict() if b else None
        with get_session_context() as session:
            b = session.scalars(stmt).first()
            return b.to_dict() if b else None

    def count(self) -> int:
        """Returns total bills count."""
        stmt = select(func.count(Bill.BillNo))
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)

    def count_by_file_id(self, file_id: int) -> int:
        """Returns bills count associated with a specific FileID."""
        stmt = select(func.count(Bill.BillNo)).where(Bill.FileID == file_id)
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)


bill_repository = BillRepository()

"""
SettlementRepository implementation using SQLAlchemy ORM.

Centralizes database operations for dbo.Settlements.
"""

from typing import Optional, List, Dict, Any
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.models.settlement import Settlement
from app.database.session import get_session_context
from app.core.logging import logger


class SettlementRepository:
    """
    Repository layer centralizing all database operations for dbo.Settlements.
    """

    def __init__(self, session: Optional[Session] = None):
        self._session = session

    def get_by_id(self, settlement_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single settlement by Settlement_ID."""
        if self._session is not None:
            s = self._session.get(Settlement, settlement_id)
            return s.to_dict() if s else None
        with get_session_context() as session:
            s = session.get(Settlement, settlement_id)
            return s.to_dict() if s else None

    def find_by_bill_no(self, bill_no: int) -> List[Dict[str, Any]]:
        """Finds settlements associated with a BillNo."""
        stmt = select(Settlement).where(Settlement.BillNo == bill_no)
        if self._session is not None:
            return [s.to_dict() for s in self._session.scalars(stmt).all()]
        with get_session_context() as session:
            return [s.to_dict() for s in session.scalars(stmt).all()]

    def count(self) -> int:
        """Returns total settlements count."""
        stmt = select(func.count(Settlement.Settlement_ID))
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)

    def count_by_file_id(self, file_id: int) -> int:
        """Returns settlements count associated with a specific FileID."""
        stmt = select(func.count(Settlement.Settlement_ID)).where(Settlement.FileID == file_id)
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)


settlement_repository = SettlementRepository()

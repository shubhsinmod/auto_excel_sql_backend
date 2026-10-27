"""
OrderRepository implementation using SQLAlchemy ORM.

Centralizes database operations for dbo.Order.
"""

from typing import Optional, List, Dict, Any
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.models.order import Order
from app.database.session import get_session_context
from app.core.logging import logger


class OrderRepository:
    """
    Repository layer centralizing all database operations for dbo.Order.
    """

    def __init__(self, session: Optional[Session] = None):
        self._session = session

    def get_by_id(self, order_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single order by Order_ID."""
        if self._session is not None:
            o = self._session.get(Order, order_id)
            return o.to_dict() if o else None
        with get_session_context() as session:
            o = session.get(Order, order_id)
            return o.to_dict() if o else None

    def find_by_bill_no(self, bill_no: int) -> List[Dict[str, Any]]:
        """Finds orders associated with a BillNo."""
        stmt = select(Order).where(Order.BillNo == bill_no)
        if self._session is not None:
            return [o.to_dict() for o in self._session.scalars(stmt).all()]
        with get_session_context() as session:
            return [o.to_dict() for o in session.scalars(stmt).all()]

    def count(self) -> int:
        """Returns total order count."""
        stmt = select(func.count(Order.Order_ID))
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)

    def count_by_file_id(self, file_id: int) -> int:
        """Returns order count associated with a specific FileID."""
        stmt = select(func.count(Order.Order_ID)).where(Order.FileID == file_id)
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)


order_repository = OrderRepository()

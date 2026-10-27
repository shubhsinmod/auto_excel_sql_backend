"""
OrderLineItemRepository implementation using SQLAlchemy ORM.

Centralizes database operations for dbo.OrderLineItems.
"""

from typing import Optional, List, Dict, Any
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.models.order_line_item import OrderLineItem
from app.database.session import get_session_context
from app.core.logging import logger


class OrderLineItemRepository:
    """
    Repository layer centralizing all database operations for dbo.OrderLineItems.
    """

    def __init__(self, session: Optional[Session] = None):
        self._session = session

    def get_by_id(self, line_item_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single order line item by LineItemID."""
        if self._session is not None:
            oli = self._session.get(OrderLineItem, line_item_id)
            return oli.to_dict() if oli else None
        with get_session_context() as session:
            oli = session.get(OrderLineItem, line_item_id)
            return oli.to_dict() if oli else None

    def find_by_order_id(self, order_id: int) -> List[Dict[str, Any]]:
        """Finds line items associated with an Order_ID."""
        stmt = select(OrderLineItem).where(OrderLineItem.Order_ID == order_id)
        if self._session is not None:
            return [oli.to_dict() for oli in self._session.scalars(stmt).all()]
        with get_session_context() as session:
            return [oli.to_dict() for oli in session.scalars(stmt).all()]

    def count(self) -> int:
        """Returns total order line items count."""
        stmt = select(func.count(OrderLineItem.LineItemID))
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)

    def count_by_file_id(self, file_id: int) -> int:
        """Returns line items count associated with a specific FileID."""
        stmt = select(func.count(OrderLineItem.LineItemID)).where(OrderLineItem.FileID == file_id)
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)


order_line_item_repository = OrderLineItemRepository()

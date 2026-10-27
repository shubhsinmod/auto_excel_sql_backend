"""
CustomerRepository implementation using SQLAlchemy ORM.

Centralizes database operations for dbo.Customer.
"""

from typing import Optional, List, Dict, Any, Callable
from sqlalchemy import select, func, text, Engine
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.database.session import get_session_context
from app.database.connection import get_engine
from app.core.logging import logger


class CustomerRepository:
    """
    Repository layer centralizing all database operations for dbo.Customer.
    Provides ORM CRUD methods and optimized batch operations.
    """

    def __init__(self, session: Optional[Session] = None):
        self._session = session

    def get_by_id(self, customer_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single customer by Customer_ID."""
        if self._session is not None:
            c = self._session.get(Customer, customer_id)
            return c.to_dict() if c else None
        with get_session_context() as session:
            c = session.get(Customer, customer_id)
            return c.to_dict() if c else None

    def find_by_mobile(self, mobile_number: str) -> List[Dict[str, Any]]:
        """Finds customers by mobile number."""
        clean_mob = (mobile_number or "").strip()
        if not clean_mob:
            return []
        stmt = select(Customer).where(Customer.Mobile_Number == clean_mob).limit(10)
        if self._session is not None:
            return [c.to_dict() for c in self._session.scalars(stmt).all()]
        with get_session_context() as session:
            return [c.to_dict() for c in session.scalars(stmt).all()]

    def count(self) -> int:
        """Returns total customer count."""
        stmt = select(func.count(Customer.Customer_ID))
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)

    def count_by_file_id(self, file_id: int) -> int:
        """Returns customer count associated with a specific FileID."""
        stmt = select(func.count(Customer.Customer_ID)).where(Customer.FileID == file_id)
        if self._session is not None:
            return int(self._session.scalar(stmt) or 0)
        with get_session_context() as session:
            return int(session.scalar(stmt) or 0)


customer_repository = CustomerRepository()

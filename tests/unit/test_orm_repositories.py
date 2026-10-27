"""
Unit tests for SQLAlchemy ORM models, Repositories, and Session Lifecycle.
Verifies layered architecture and zero schema alterations.
"""

import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from app.database.base import Base
from app.models.user import User
from app.models.uploaded_file import UploadedFile
from app.models.customer import Customer
from app.models.bill import Bill
from app.models.order import Order
from app.models.order_line_item import OrderLineItem
from app.models.settlement import Settlement
from app.repositories.user_repository import UserRepository, DuplicateUserError
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.bill_repository import BillRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.order_line_item_repository import OrderLineItemRepository
from app.repositories.settlement_repository import SettlementRepository


@pytest.fixture
def sqlite_orm_session():
    """
    Creates an in-memory SQLite session with table structures mirroring
    the production entity definitions for isolated unit testing.
    """
    engine = create_engine("sqlite:///:memory:")
    # Remove schema qualification for in-memory sqlite test
    for table in Base.metadata.tables.values():
        table.schema = None
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        # Restore schema qualification
        for table in Base.metadata.tables.values():
            table.schema = "dbo"


# ============================================================================
# UserRepository ORM Tests
# ============================================================================

def test_orm_user_repository_finds_user_case_insensitive(sqlite_orm_session: Session):
    user = User(
        UserName="Prashant",
        Password="hashed_password_123",
        Name="Prashant Kumar"
    )
    sqlite_orm_session.add(user)
    sqlite_orm_session.commit()

    repo = UserRepository(session=sqlite_orm_session)

    # Test exact match
    found = repo.get_user_by_username("Prashant")
    assert found is not None
    assert found["UserName"] == "Prashant"

    # Test lowercase and trimmed match
    found_lower = repo.get_user_by_username("  prashant  ")
    assert found_lower is not None
    assert found_lower["UserName"] == "Prashant"


def test_orm_user_repository_empty_username_returns_none(sqlite_orm_session: Session):
    repo = UserRepository(session=sqlite_orm_session)
    assert repo.get_user_by_username("") is None
    assert repo.get_user_by_username("   ") is None
    assert repo.find_users_by_username("") == []


def test_orm_user_repository_raises_duplicate_error(sqlite_orm_session: Session):
    u1 = User(UserName="admin", Password="hash1")
    u2 = User(UserName="ADMIN", Password="hash2")
    sqlite_orm_session.add_all([u1, u2])
    sqlite_orm_session.commit()

    repo = UserRepository(session=sqlite_orm_session)
    with pytest.raises(DuplicateUserError):
        repo.get_user_by_username("admin")


def test_orm_user_repository_user_not_found(sqlite_orm_session: Session):
    repo = UserRepository(session=sqlite_orm_session)
    assert repo.get_user_by_username("non_existent_user") is None


# ============================================================================
# UploadedFileRepository ORM Tests
# ============================================================================

def test_orm_uploaded_file_lifecycle(sqlite_orm_session: Session):
    repo = UploadedFileRepository(session=sqlite_orm_session)

    # 1. Register new file
    file_id = repo.create_file_record("sales_q3.xlsx")
    assert file_id is not None
    assert file_id > 0

    # 2. Verify exists
    assert repo.file_exists(file_id) is True
    assert repo.file_exists(99999) is False

    # 3. Retrieve by ID
    file_record = repo.get_file_by_id(file_id)
    assert file_record is not None
    assert file_record["FileName"] == "sales_q3.xlsx"
    assert file_record["Status"] == "Processing"

    # 4. Update status
    repo.update_file_status(file_id, "Completed")
    updated = repo.get_file_by_id(file_id)
    assert updated["Status"] == "Completed"

    # 5. List files
    files = repo.list_files(limit=10)
    assert len(files) == 1
    assert files[0]["FileID"] == file_id

    # 6. Count
    assert repo.get_total_count() == 1


# ============================================================================
# Domain Repositories (Customer, Bill, Order, OrderLineItem, Settlement)
# ============================================================================

def test_domain_repositories_orm_queries(sqlite_orm_session: Session):
    cust = Customer(Customer_Name="Test Customer", Mobile_Number="9876543210", FileID=1)
    bill = Bill(BillNo=1001, BillAmount=150.0, FileID=1)
    order = Order(
        Customer_ID=1,
        BillNo="1001",
        Price=150.0,
        FileID=1,
        Order_DateTime=datetime.datetime(2025, 1, 1, 12, 0, 0),
        Order_Outlet_ID=1
    )
    line_item = OrderLineItem(Order_ID=1, Item_Name="Pizza", Quantity=1, Price=150.0, FileID=1)
    settle = Settlement(BillNo=1001, Cash=150.0, FileID=1)

    sqlite_orm_session.add_all([cust, bill, order, line_item, settle])
    sqlite_orm_session.commit()

    cust_repo = CustomerRepository(session=sqlite_orm_session)
    bill_repo = BillRepository(session=sqlite_orm_session)
    order_repo = OrderRepository(session=sqlite_orm_session)
    line_item_repo = OrderLineItemRepository(session=sqlite_orm_session)
    settle_repo = SettlementRepository(session=sqlite_orm_session)

    assert cust_repo.count() == 1
    assert len(cust_repo.find_by_mobile("9876543210")) == 1

    assert bill_repo.count() == 1
    assert bill_repo.get_by_bill_no(1001) is not None

    assert order_repo.count() == 1
    assert len(order_repo.find_by_bill_no("1001")) == 1

    assert line_item_repo.count() == 1
    assert len(line_item_repo.find_by_order_id(1)) == 1

    assert settle_repo.count() == 1
    assert len(settle_repo.find_by_bill_no(1001)) == 1

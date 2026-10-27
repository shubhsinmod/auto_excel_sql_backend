"""
Bill SQLAlchemy ORM entity.

Maps directly to dbo.Bills in Microsoft SQL Server without altering existing schema.
"""

from sqlalchemy import Column, BigInteger, Integer, String, DateTime, Numeric
from app.database.base import Base


class Bill(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.Bills table.
    """
    __tablename__ = "Bills"
    __table_args__ = {"schema": "dbo"}

    BillNo = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    BillAmount = Column(Numeric(19, 4), nullable=True)
    Discount = Column(Numeric(19, 4), nullable=True)
    OtherCharges = Column(Numeric(19, 4), nullable=True)
    VAT = Column(Numeric(19, 4), nullable=True)
    FinalAmount = Column(Numeric(19, 4), nullable=True)
    Bill_Date = Column(DateTime, nullable=True)
    Order_ID = Column(BigInteger, nullable=True)
    DinningOrder_ID = Column(BigInteger, nullable=True)
    Order_Type = Column(String(50), nullable=True)
    Settlement_Type = Column(String(50), nullable=True)
    OtherDetails = Column(String(250), nullable=True)
    Outlet_ID = Column(Integer, nullable=True)
    Bill_Number = Column(BigInteger, nullable=True)
    FileID = Column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "BillNo": self.BillNo,
            "BillAmount": float(self.BillAmount) if self.BillAmount is not None else None,
            "Discount": float(self.Discount) if self.Discount is not None else None,
            "OtherCharges": float(self.OtherCharges) if self.OtherCharges is not None else None,
            "VAT": float(self.VAT) if self.VAT is not None else None,
            "FinalAmount": float(self.FinalAmount) if self.FinalAmount is not None else None,
            "Bill_Date": self.Bill_Date.isoformat() if self.Bill_Date else None,
            "Order_ID": self.Order_ID,
            "DinningOrder_ID": self.DinningOrder_ID,
            "Order_Type": self.Order_Type,
            "Settlement_Type": self.Settlement_Type,
            "OtherDetails": self.OtherDetails,
            "Outlet_ID": self.Outlet_ID,
            "Bill_Number": self.Bill_Number,
            "FileID": self.FileID
        }

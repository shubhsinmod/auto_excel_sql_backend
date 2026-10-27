"""
OrderLineItem SQLAlchemy ORM entity.

Maps directly to dbo.OrderLineItems in Microsoft SQL Server without altering existing schema.
"""

from sqlalchemy import Column, BigInteger, Integer, String, DateTime, Numeric
from sqlalchemy.orm import synonym
from app.database.base import Base


class OrderLineItem(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.OrderLineItems table.
    """
    __tablename__ = "OrderLineItems"
    __table_args__ = {"schema": "dbo"}

    Line_ID = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    Order_ID = Column(BigInteger, nullable=True)
    Item_ID = Column(String(50), nullable=True)
    Item_Price = Column(Numeric(19, 4), nullable=True)
    Quantity = Column(Integer, nullable=True)
    Description = Column(String(200), nullable=True)
    IsComposite = Column(String(50), nullable=True)
    GroupID = Column(String(100), nullable=True)
    StopPrint = Column(Integer, nullable=True)
    isServed = Column(Integer, nullable=True)
    KOT_Time = Column(DateTime, nullable=True)
    ID = Column(Integer, nullable=True)
    Parent_ID = Column(Integer, nullable=True)
    FileID = Column(Integer, nullable=True)

    # Synonyms for seamless backwards compatibility
    LineItemID = synonym("Line_ID")
    Item_Name = synonym("Description")
    Price = synonym("Item_Price")

    def to_dict(self) -> dict:
        return {
            "Line_ID": self.Line_ID,
            "LineItemID": self.Line_ID,
            "Order_ID": self.Order_ID,
            "Item_ID": self.Item_ID,
            "Item_Price": float(self.Item_Price) if self.Item_Price is not None else None,
            "Price": float(self.Item_Price) if self.Item_Price is not None else None,
            "Quantity": self.Quantity,
            "Description": self.Description,
            "Item_Name": self.Description,
            "IsComposite": self.IsComposite,
            "GroupID": self.GroupID,
            "StopPrint": self.StopPrint,
            "isServed": self.isServed,
            "KOT_Time": self.KOT_Time.isoformat() if self.KOT_Time else None,
            "ID": self.ID,
            "Parent_ID": self.Parent_ID,
            "FileID": self.FileID
        }

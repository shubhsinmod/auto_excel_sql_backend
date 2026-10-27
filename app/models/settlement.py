"""
Settlement SQLAlchemy ORM entity.

Maps directly to dbo.Settlements in Microsoft SQL Server without altering existing schema.
"""

from sqlalchemy import Column, BigInteger, Integer, String, Numeric
from app.database.base import Base


class Settlement(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.Settlements table.
    """
    __tablename__ = "Settlements"
    __table_args__ = {"schema": "dbo"}

    Settlement_ID = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    BillNo = Column(BigInteger, nullable=True)
    Cash = Column(Numeric(19, 4), nullable=True)
    CreditCard = Column(Numeric(19, 4), nullable=True)
    Sodexo = Column(Numeric(19, 4), nullable=True)
    Cheque = Column(Numeric(19, 4), nullable=True)
    Complementary = Column(Numeric(19, 4), nullable=True)
    Others = Column(Numeric(19, 4), nullable=True)
    Service_Failure = Column(Numeric(19, 4), nullable=True)
    DebitCard = Column(Numeric(19, 4), nullable=True)
    TicketRestaurant = Column(Numeric(19, 4), nullable=True)
    GE = Column(Numeric(19, 4), nullable=True)
    BO = Column(Numeric(19, 4), nullable=True)
    Return = Column(Numeric(19, 4), nullable=True)
    OtherSettlement_Name = Column(String(200), nullable=True)
    FileID = Column(Integer, nullable=True)
    Outlet_ID = Column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "Settlement_ID": self.Settlement_ID,
            "BillNo": self.BillNo,
            "Cash": float(self.Cash) if self.Cash is not None else None,
            "CreditCard": float(self.CreditCard) if self.CreditCard is not None else None,
            "Sodexo": float(self.Sodexo) if self.Sodexo is not None else None,
            "Cheque": float(self.Cheque) if self.Cheque is not None else None,
            "Complementary": float(self.Complementary) if self.Complementary is not None else None,
            "Others": float(self.Others) if self.Others is not None else None,
            "Service_Failure": float(self.Service_Failure) if self.Service_Failure is not None else None,
            "DebitCard": float(self.DebitCard) if self.DebitCard is not None else None,
            "TicketRestaurant": float(self.TicketRestaurant) if self.TicketRestaurant is not None else None,
            "GE": float(self.GE) if self.GE is not None else None,
            "BO": float(self.BO) if self.BO is not None else None,
            "Return": float(self.Return) if self.Return is not None else None,
            "OtherSettlement_Name": self.OtherSettlement_Name,
            "FileID": self.FileID,
            "Outlet_ID": self.Outlet_ID
        }

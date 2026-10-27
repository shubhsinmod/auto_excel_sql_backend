"""
Customer SQLAlchemy ORM entity.

Maps directly to dbo.Customer in Microsoft SQL Server without altering existing schema.
"""

from sqlalchemy import Column, BigInteger, Integer, String, DateTime
from app.database.base import Base


class Customer(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.Customer table.
    """
    __tablename__ = "Customer"
    __table_args__ = {"schema": "dbo"}

    Customer_ID = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    Customer_Name = Column(String(100), nullable=False)
    Address = Column(String(500), nullable=True)
    Location = Column(String(100), nullable=True)
    Contact_Number = Column(String(50), nullable=True)
    Mobile_Number = Column(String(50), nullable=True)
    Date_Added = Column(DateTime, nullable=True)
    Date_Modified = Column(DateTime, nullable=True)
    Outlet_ID = Column(Integer, nullable=True)
    Comments = Column(String(1000), nullable=True)
    Email_ID = Column(String(100), nullable=True)
    Pincode = Column(String(50), nullable=True)
    UserName = Column(String(50), nullable=True)
    Password = Column(String(500), nullable=True)
    Column1 = Column(String(100), nullable=True)
    Column2 = Column(String(100), nullable=True)
    FileID = Column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "Customer_ID": self.Customer_ID,
            "Customer_Name": self.Customer_Name,
            "Address": self.Address,
            "Location": self.Location,
            "Contact_Number": self.Contact_Number,
            "Mobile_Number": self.Mobile_Number,
            "Date_Added": self.Date_Added.isoformat() if self.Date_Added else None,
            "Date_Modified": self.Date_Modified.isoformat() if self.Date_Modified else None,
            "Outlet_ID": self.Outlet_ID,
            "Comments": self.Comments,
            "Email_ID": self.Email_ID,
            "Pincode": self.Pincode,
            "UserName": self.UserName,
            "Password": self.Password,
            "Column1": self.Column1,
            "Column2": self.Column2,
            "FileID": self.FileID
        }

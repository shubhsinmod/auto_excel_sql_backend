"""
Order SQLAlchemy ORM entity.

Maps directly to dbo.Order in Microsoft SQL Server without altering existing schema.
"""

from sqlalchemy import Column, BigInteger, Integer, String, DateTime, Numeric, Float
from app.database.base import Base


class Order(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.Order table.
    """
    __tablename__ = "Order"
    __table_args__ = {"schema": "dbo"}

    Order_ID = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    Customer_ID = Column(BigInteger, nullable=True)
    Quantity = Column(Integer, nullable=True)
    Order_DateTime = Column(DateTime, nullable=False)
    Order_Outlet_ID = Column(Integer, nullable=False)
    Order_Taken_By = Column(String(100), nullable=True)
    Price = Column(Numeric(19, 4), nullable=True)
    Tax = Column(Numeric(19, 4), nullable=True)
    Assigned_Outlet_ID = Column(Integer, nullable=True)
    Delivered = Column(Integer, nullable=True)
    Delivery_DateTime = Column(DateTime, nullable=True)
    Delivered_By = Column(String(100), nullable=True)
    Despatched = Column(Integer, nullable=True)
    Despatched_DateTime = Column(DateTime, nullable=True)
    BillNo = Column(String(50), nullable=True)
    DeliveryDateCommitted = Column(DateTime, nullable=True)
    Time_Committed = Column(String(50), nullable=True)
    Customer_Comments = Column(String(500), nullable=True)
    Party_Order = Column(Integer, nullable=True)
    Discount_Type = Column(String(50), nullable=True)
    Discount_Value = Column(String(50), nullable=True)
    Total_Discount = Column(Numeric(19, 4), nullable=True)
    Other_Charges = Column(Numeric(19, 4), nullable=True)
    Column1 = Column(String(100), nullable=True)
    Column2 = Column(String(100), nullable=True)
    Service_Guarantee = Column(Integer, nullable=True)
    Party_Order_Marked = Column(Integer, nullable=True)
    Packed = Column(Integer, nullable=True)
    Packed_DateTime = Column(DateTime, nullable=True)
    Service_Failed = Column(Integer, nullable=True)
    Service_Fail_Reason = Column(String(500), nullable=True)
    VAT_Total = Column(Numeric(19, 4), nullable=True)
    Service_Tax_Total = Column(Numeric(19, 4), nullable=True)
    Voucher_Message = Column(String(300), nullable=True)
    Order_Type = Column(String(50), nullable=True)
    TimeTaken = Column(Float, nullable=True)
    Service_Charge_Total = Column(Numeric(19, 4), nullable=True)
    Other_Tax_Total = Column(Numeric(19, 4), nullable=True)
    TableNo = Column(String(100), nullable=True)
    No_Of_Pax = Column(Integer, nullable=True)
    Waiter = Column(String(100), nullable=True)
    Order_Source = Column(String(100), nullable=True)
    Source_Order_ID = Column(String(100), nullable=True)
    UP_Order_ID = Column(String(100), nullable=True)
    Online_Order_ID = Column(BigInteger, nullable=True)
    Order_Status = Column(String(200), nullable=True)
    Brand = Column(String(100), nullable=True)
    FileID = Column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "Order_ID": self.Order_ID,
            "Customer_ID": self.Customer_ID,
            "Quantity": self.Quantity,
            "Order_DateTime": self.Order_DateTime.isoformat() if self.Order_DateTime else None,
            "Order_Outlet_ID": self.Order_Outlet_ID,
            "Order_Taken_By": self.Order_Taken_By,
            "Price": float(self.Price) if self.Price is not None else None,
            "Tax": float(self.Tax) if self.Tax is not None else None,
            "Assigned_Outlet_ID": self.Assigned_Outlet_ID,
            "Delivered": self.Delivered,
            "Delivery_DateTime": self.Delivery_DateTime.isoformat() if self.Delivery_DateTime else None,
            "Delivered_By": self.Delivered_By,
            "Despatched": self.Despatched,
            "Despatched_DateTime": self.Despatched_DateTime.isoformat() if self.Despatched_DateTime else None,
            "BillNo": self.BillNo,
            "DeliveryDateCommitted": self.DeliveryDateCommitted.isoformat() if self.DeliveryDateCommitted else None,
            "Time_Committed": self.Time_Committed,
            "Customer_Comments": self.Customer_Comments,
            "Party_Order": self.Party_Order,
            "Discount_Type": self.Discount_Type,
            "Discount_Value": self.Discount_Value,
            "Total_Discount": float(self.Total_Discount) if self.Total_Discount is not None else None,
            "Other_Charges": float(self.Other_Charges) if self.Other_Charges is not None else None,
            "Column1": self.Column1,
            "Column2": self.Column2,
            "Service_Guarantee": self.Service_Guarantee,
            "Party_Order_Marked": self.Party_Order_Marked,
            "Packed": self.Packed,
            "Packed_DateTime": self.Packed_DateTime.isoformat() if self.Packed_DateTime else None,
            "Service_Failed": self.Service_Failed,
            "Service_Fail_Reason": self.Service_Fail_Reason,
            "VAT_Total": float(self.VAT_Total) if self.VAT_Total is not None else None,
            "Service_Tax_Total": float(self.Service_Tax_Total) if self.Service_Tax_Total is not None else None,
            "Voucher_Message": self.Voucher_Message,
            "Order_Type": self.Order_Type,
            "TimeTaken": self.TimeTaken,
            "Service_Charge_Total": float(self.Service_Charge_Total) if self.Service_Charge_Total is not None else None,
            "Other_Tax_Total": float(self.Other_Tax_Total) if self.Other_Tax_Total is not None else None,
            "TableNo": self.TableNo,
            "No_Of_Pax": self.No_Of_Pax,
            "Waiter": self.Waiter,
            "Order_Source": self.Order_Source,
            "Source_Order_ID": self.Source_Order_ID,
            "UP_Order_ID": self.UP_Order_ID,
            "Online_Order_ID": self.Online_Order_ID,
            "Order_Status": self.Order_Status,
            "Brand": self.Brand,
            "FileID": self.FileID
        }

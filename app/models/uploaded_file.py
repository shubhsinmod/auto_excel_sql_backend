"""
UploadedFile SQLAlchemy ORM entity.

Maps directly to dbo.UploadedFiles in Microsoft SQL Server.
"""

import datetime
from sqlalchemy import Column, Integer, String, DateTime
from app.database.base import Base


class UploadedFile(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.UploadedFiles table.
    """
    __tablename__ = "UploadedFiles"
    __table_args__ = {"schema": "dbo"}

    FileID = Column(Integer, primary_key=True, autoincrement=True)
    FileName = Column(String(255), nullable=False)
    UploadedAt = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    Status = Column(String(30), nullable=False, default="Processing")

    def to_dict(self) -> dict:
        uploaded_at_str = self.UploadedAt.isoformat() if isinstance(self.UploadedAt, (datetime.datetime, datetime.date)) else str(self.UploadedAt or "")
        return {
            "FileID": self.FileID,
            "FileName": self.FileName,
            "UploadedAt": uploaded_at_str,
            "Status": self.Status
        }

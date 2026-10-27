from typing import Optional
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String
from app.database.base import Base


class User(Base):
    """
    SQLAlchemy ORM Entity mapped to existing dbo.Users table.
    """
    __tablename__ = "Users"
    __table_args__ = {"schema": "dbo"}

    UserID = Column(Integer, primary_key=True, autoincrement=True)
    UserName = Column(String(50), nullable=False)
    Name = Column(String(200), nullable=True)
    Password = Column(String(500), nullable=False)
    UserType = Column(Integer, nullable=True)
    Role_ID = Column(Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "UserID": self.UserID,
            "UserName": self.UserName,
            "Name": self.Name,
            "UserType": self.UserType,
            "Role_ID": self.Role_ID
        }


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    authenticated: bool
    username: str


class UserRecord(BaseModel):
    username: str
    password_hash: str
    is_active: bool = True

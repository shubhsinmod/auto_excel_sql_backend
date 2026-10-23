from typing import Optional
from pydantic import BaseModel, Field


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

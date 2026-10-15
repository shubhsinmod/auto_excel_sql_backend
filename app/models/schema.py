from typing import List, Optional, Any
from pydantic import BaseModel, Field


class ForeignKeyInfo(BaseModel):
    """
    Represents a Foreign Key relationship targeting another table and column.
    """
    constrained_column: str
    target_table: str
    target_column: str
    target_schema: str = "dbo"


class ColumnMetadata(BaseModel):
    """
    Represents metadata for a single database table column.
    """
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    identity: bool = False
    default: Optional[str] = None
    unique: bool = False
    foreign_keys: List[ForeignKeyInfo] = Field(default_factory=list)
    max_length: Optional[int] = None


class TableMetadata(BaseModel):
    """
    Represents metadata for a single database table.
    """
    name: str
    schema_name: str = "dbo"
    columns: List[ColumnMetadata] = Field(default_factory=list)
    primary_keys: List[str] = Field(default_factory=list)
    foreign_keys: List[ForeignKeyInfo] = Field(default_factory=list)


class DatabaseSchemaMetadata(BaseModel):
    """
    Represents the full introspected schema of the connected database.
    """
    database_name: str
    db_type: str
    tables: List[TableMetadata] = Field(default_factory=list)


class ConnectionTestResponse(BaseModel):
    """Response model for database connection status check."""
    connected: bool
    database: str = ""
    db_type: str = "mssql"
    server: Optional[str] = None
    version: Optional[str] = None
    message: str = ""
    error: Optional[str] = None


class DatabaseConnectRequest(BaseModel):
    """Backward-compatible request model for testing a SQL Server connection."""
    server: str
    database: str
    username: str = ""
    password: str = ""
    port: Optional[int] = None
    driver: str = "ODBC Driver 17 for SQL Server"
    trusted_connection: bool = False

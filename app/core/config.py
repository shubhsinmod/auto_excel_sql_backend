# import urllib.parse
# from pathlib import Path
# from typing import Optional
# from pydantic import Field
# from pydantic_settings import BaseSettings, SettingsConfigDict

# ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


# class Settings(BaseSettings):
#     """
#     Application Settings loaded from environment variables or .env file.
#     Supports MySQL, Microsoft SQL Server, PostgreSQL, and SQLite.
#     """
#     model_config = SettingsConfigDict(
#         env_file=[str(ENV_PATH), ".env"],
#         env_file_encoding="utf-8",
#         extra="ignore"
#     )

#     # Database Configuration
#     DB_TYPE: str = Field(default="mssql", description="Database dialect: mysql, mssql, postgresql, sqlite")
#     DB_HOST: str = Field(default="KARTHIKSIGMOSS", alias="DB_SERVER")
#     DB_PORT: Optional[int] = Field(default=None)
#     DB_NAME: str = Field(default="master")
#     DB_USER: Optional[str] = Field(default="sa", alias="DB_USERNAME")
#     DB_PASSWORD: Optional[str] = Field(default="")
#     DB_DRIVER: str = Field(default="ODBC Driver 17 for SQL Server")
#     DB_TRUSTED_CONNECTION: bool = Field(default=False)

#     # Application Configuration
#     APP_ENV: str = Field(default="development")
#     LOG_LEVEL: str = Field(default="INFO")
#     SECRET_KEY: str = Field(default="dev-secret-key-123")

#     @property
#     def database_url(self) -> str:
#         """
#         Dynamically constructs the SQLAlchemy database URL based on DB_TYPE.
#         """
#         db_type = self.DB_TYPE.lower()
#         clean_db_name = self.DB_NAME.strip("[]") if self.DB_NAME else ""
#         password_encoded = urllib.parse.quote_plus(self.DB_PASSWORD) if self.DB_PASSWORD else ""
#         user = self.DB_USER or ""

#         if db_type == "mysql":
#             port = self.DB_PORT if self.DB_PORT else 3306
#             if user and password_encoded:
#                 user_pass = f"{user}:{password_encoded}@"
#             elif user:
#                 user_pass = f"{user}@"
#             else:
#                 user_pass = ""
#             return f"mysql+pymysql://{user_pass}{self.DB_HOST}:{port}/{clean_db_name}?charset=utf8mb4"

#         elif db_type in ("mssql", "sqlserver"):
#             port_str = f":{self.DB_PORT}" if self.DB_PORT else ""
#             driver_encoded = urllib.parse.quote_plus(self.DB_DRIVER)
#             if self.DB_TRUSTED_CONNECTION:
#                 return f"mssql+pyodbc://{self.DB_HOST}{port_str}/{clean_db_name}?driver={driver_encoded}&trusted_connection=yes"
#             return f"mssql+pyodbc://{user}:{password_encoded}@{self.DB_HOST}{port_str}/{clean_db_name}?driver={driver_encoded}"

#         elif db_type in ("postgres", "postgresql"):
#             port = self.DB_PORT if self.DB_PORT else 5432
#             return f"postgresql://{user}:{password_encoded}@{self.DB_HOST}:{port}/{self.DB_NAME}"

#         elif db_type == "sqlite":
#             return f"sqlite:///{self.DB_NAME}"

#         else:
#             raise ValueError(f"Unsupported DB_TYPE: {self.DB_TYPE}")


# settings = Settings()
import urllib.parse
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """
    Application Settings loaded from environment variables or .env file.
    Supports MySQL, Microsoft SQL Server, PostgreSQL, and SQLite.
    """
    model_config = SettingsConfigDict(
        env_file=[str(ENV_PATH), ".env"],
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Database Configuration
    DB_TYPE: str = Field(default="mssql", description="Database dialect: mysql, mssql, postgresql, sqlite")
    DB_HOST: str = Field(default="KARTHIKSIGMOSS", alias="DB_SERVER")
    DB_PORT: Optional[int] = Field(default=None)
    DB_NAME: str = Field(default="master")
    DB_USER: Optional[str] = Field(default="sa", alias="DB_USERNAME")
    DB_PASSWORD: Optional[str] = Field(default="")
    DB_DRIVER: str = Field(default="ODBC Driver 17 for SQL Server")
    DB_TRUSTED_CONNECTION: bool = Field(default=False)
    DATABASE_URL: Optional[str] = Field(default=None, description="Full connection string override if provided directly.")

    # Application Configuration
    APP_ENV: str = Field(default="development")
    LOG_LEVEL: str = Field(default="INFO")
    ENABLE_CONSOLE_LOGS: bool = Field(default=False, description="Whether to output logs to terminal/console.")
    SECRET_KEY: str = Field(default="dev-secret-key-123")
    DRY_RUN_MODE: bool = Field(default=False, description="Global dry-run test mode flag. Disabled by default.")
    DRY_RUN_MASK_SENSITIVE: bool = Field(default=True, description="Mask phone numbers and sensitive data in logs by default.")
    ENABLE_MASTER_PRICE_MAPPING: bool = Field(default=True, description="When enabled, OrderLineItems.Item_Price is populated from ItemsMaster.Item_Price for resolved items.")

    @property
    def database_url(self) -> str:
        """
        Dynamically constructs the SQLAlchemy database URL based on DB_TYPE,
        or returns DATABASE_URL if explicitly provided.
        """
        if self.DATABASE_URL:
            raw_url = self.DATABASE_URL.strip()
            # Normalize postgres:// to postgresql+psycopg:// or postgresql+psycopg2://
            if raw_url.startswith("postgres://"):
                try:
                    import psycopg
                    return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
                except ImportError:
                    return raw_url.replace("postgres://", "postgresql+psycopg2://", 1)
            elif raw_url.startswith("postgresql://") and not raw_url.startswith("postgresql+"):
                try:
                    import psycopg
                    return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
                except ImportError:
                    return raw_url.replace("postgresql://", "postgresql+psycopg2://", 1)
            return raw_url

        db_type = self.DB_TYPE.lower()
        clean_db_name = self.DB_NAME.strip("[]") if self.DB_NAME else ""
        password_encoded = urllib.parse.quote_plus(self.DB_PASSWORD) if self.DB_PASSWORD else ""
        user = self.DB_USER or ""

        if db_type == "mysql":
            port = self.DB_PORT if self.DB_PORT else 3306
            if user and password_encoded:
                user_pass = f"{user}:{password_encoded}@"
            elif user:
                user_pass = f"{user}@"
            else:
                user_pass = ""
            return f"mysql+pymysql://{user_pass}{self.DB_HOST}:{port}/{clean_db_name}?charset=utf8mb4"

        elif db_type in ("mssql", "sqlserver"):
            port_str = f":{self.DB_PORT}" if self.DB_PORT else ""
            driver_encoded = urllib.parse.quote_plus(self.DB_DRIVER)
            if self.DB_TRUSTED_CONNECTION:
                return f"mssql+pyodbc://{self.DB_HOST}{port_str}/{clean_db_name}?driver={driver_encoded}&trusted_connection=yes"
            return f"mssql+pyodbc://{user}:{password_encoded}@{self.DB_HOST}{port_str}/{clean_db_name}?driver={driver_encoded}"

        elif db_type in ("postgres", "postgresql"):
            port = self.DB_PORT if self.DB_PORT else 5432
            auth = f"{user}:{password_encoded}@" if (user and password_encoded) else (f"{user}@" if user else "")
            try:
                import psycopg
                driver = "postgresql+psycopg"
            except ImportError:
                driver = "postgresql+psycopg2"
            return f"{driver}://{auth}{self.DB_HOST}:{port}/{clean_db_name}"

        elif db_type == "sqlite":
            return f"sqlite:///{self.DB_NAME}"

        else:
            raise ValueError(f"Unsupported DB_TYPE: {self.DB_TYPE}")


settings = Settings()
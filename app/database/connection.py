import time
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.core.logging import logger

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _create_engine_options(database_url: str) -> dict:
    """Common pooling options; enable fast_executemany only for MSSQL+pyodbc."""
    options = {
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "echo": False,
    }
    # SQLAlchemy's fast_executemany is supported by the mssql+pyodbc dialect.
    # It benefits executemany/bulk parameter calls, dramatically reducing ODBC roundtrips.
    if str(database_url).lower().startswith("mssql+pyodbc://"):
        options["fast_executemany"] = True
    return options


def auto_create_database(active_settings) -> bool:
    """
    Attempts to automatically create the database on the target server if it does not exist.
    """
    db_type = active_settings.DB_TYPE.lower()
    db_name = active_settings.DB_NAME
    host = "127.0.0.1" if active_settings.DB_HOST == "localhost" else active_settings.DB_HOST

    if db_type == "mysql":
        try:
            import pymysql
            logger.info(f"[DB] Auto-create check START | host={host} db={db_name}")
            t0 = time.time()
            conn = pymysql.connect(
                host=host,
                port=active_settings.DB_PORT or 3306,
                user=active_settings.DB_USER or "root",
                password=active_settings.DB_PASSWORD or "",
                charset="utf8mb4"
            )
            with conn.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4;")
            conn.commit()
            conn.close()
            t1 = time.time()
            logger.info(f"[DB] Auto-create check END | {t1 - t0:.3f}s")
            return True
        except Exception as e:
            logger.warning(f"[DB] Auto-create check FAILED: {str(e)}")
            return False

    return False


def get_engine(override_settings=None) -> Engine:
    """
    Returns or creates the global SQLAlchemy Engine.
    """
    global _engine
    active_settings = override_settings or settings

    if override_settings:
        t0 = time.time()
        logger.info(f"[DB] create_engine (temporary) START | URL: {active_settings.database_url}")
        engine = create_engine(
            active_settings.database_url,
            **_create_engine_options(active_settings.database_url)
        )
        t1 = time.time()
        logger.info(f"[DB] create_engine (temporary) END | {t1 - t0:.3f}s")
        return engine

    if _engine is None:
        t0 = time.time()
        logger.info(f"[DB] create_engine START | DB_TYPE={active_settings.DB_TYPE}, DB_NAME={active_settings.DB_NAME}")
        _engine = create_engine(
            active_settings.database_url,
            **_create_engine_options(active_settings.database_url)
        )
        t1 = time.time()
        logger.info(f"[DB] create_engine END | {t1 - t0:.3f}s")

    return _engine


def get_session_factory() -> sessionmaker:
    """
    Returns the global session factory.
    """
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return _SessionLocal


def get_db_session() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a Database Session.
    """
    SessionFactory = get_session_factory()
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


def test_db_connection(custom_settings=None) -> tuple[bool, str]:
    """
    Tests connectivity to the configured database with precise timing logs.
    """
    active_settings = custom_settings or settings
    try:
        t0 = time.time()
        logger.info("[DB] test_db_connection START")
        
        t_eng_0 = time.time()
        engine = get_engine(custom_settings)
        t_eng_1 = time.time()

        t_conn_0 = time.time()
        logger.info("[DB] connection START")
        with engine.connect() as conn:
            t_conn_1 = time.time()
            logger.info(f"[DB] connection END | {t_conn_1 - t_conn_0:.3f}s")

            t_sel_0 = time.time()
            logger.info("[DB] SELECT 1 START")
            conn.execute(text("SELECT 1"))
            t_sel_1 = time.time()
            logger.info(f"[DB] SELECT 1 END | {t_sel_1 - t_sel_0:.3f}s")

        t1 = time.time()
        logger.info(f"[DB] test_db_connection END | TOTAL={t1 - t0:.3f}s")
        return True, "Successfully connected to database."
    except Exception as e:
        err_msg = str(e)
        logger.error(f"[DB] Database connection error: {err_msg}")

        # Check for 1049 Unknown Database error in MySQL
        if "1049" in err_msg or "Unknown database" in err_msg:
            logger.info("[DB] Database missing. Attempting automatic creation...")
            created = auto_create_database(active_settings)
            if created:
                try:
                    engine = get_engine(custom_settings)
                    with engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
                    return True, f"Successfully created and connected to database '{active_settings.DB_NAME}'."
                except Exception as retry_err:
                    return False, f"Database created, but reconnection failed: {str(retry_err)}"

        return False, f"Connection failed: {err_msg}"


class DatabaseManager:
    """Compatibility facade for API/tests that need ad-hoc database connections.

    Supports MSSQL, PostgreSQL, MySQL, and SQLite. Credentials are
    never included in returned error messages.
    """
    @staticmethod
    def create_custom_engine(server: str = "localhost", database: str = "", username: str = "",
                             password: str = "", port: int | None = None,
                             driver: str = "ODBC Driver 17 for SQL Server",
                             trusted_connection: bool = False, db_type: str = "mssql") -> Engine:
        from urllib.parse import quote_plus
        from sqlalchemy.engine import URL

        db_type_lower = (db_type or "mssql").lower()
        password_encoded = quote_plus(password) if password else ""
        clean_db = (database or "").strip("[]")

        if db_type_lower in ("postgres", "postgresql"):
            p = port or 5432
            auth = f"{username}:{password_encoded}@" if (username and password_encoded) else (f"{username}@" if username else "")
            return create_engine(f"postgresql+psycopg2://{auth}{server}:{p}/{clean_db}", pool_pre_ping=True)

        elif db_type_lower in ("mysql", "mariadb"):
            p = port or 3306
            auth = f"{username}:{password_encoded}@" if (username and password_encoded) else (f"{username}@" if username else "")
            return create_engine(f"mysql+pymysql://{auth}{server}:{p}/{clean_db}?charset=utf8mb4", pool_pre_ping=True)

        elif db_type_lower == "sqlite":
            return create_engine(f"sqlite:///{clean_db}", pool_pre_ping=True)

        else:
            query = {"driver": driver}
            if trusted_connection:
                query["trusted_connection"] = "yes"
                return create_engine(URL.create(
                    "mssql+pyodbc", host=server, port=port, database=clean_db, query=query
                ), pool_pre_ping=True, fast_executemany=True)
            return create_engine(URL.create(
                "mssql+pyodbc", username=username, password=password, host=server,
                port=port, database=clean_db, query=query
            ), pool_pre_ping=True, fast_executemany=True)

    @classmethod
    def test_connection(cls, server: str = "localhost", database: str = "", username: str = "",
                        password: str = "", port: int | None = None,
                        driver: str = "ODBC Driver 17 for SQL Server",
                        trusted_connection: bool = False, db_type: str = "mssql") -> dict:
        engine = None
        db_type_lower = (db_type or "mssql").lower()
        try:
            engine = cls.create_custom_engine(
                server=server, database=database, username=username, password=password,
                port=port, driver=driver, trusted_connection=trusted_connection, db_type=db_type
            )
            with engine.connect() as conn:
                version_sql = text("SELECT version()") if db_type_lower in ("postgres", "postgresql", "mysql") else text("SELECT @@VERSION")
                version = conn.execute(version_sql).scalar()
            return {"connected": True, "database": database, "server": server,
                    "version": str(version or ""), "message": "Connected successfully", "error": None,
                    "db_type": db_type_lower}
        except Exception as exc:
            message = str(exc).replace(password, "[REDACTED]") if password else str(exc)
            return {"connected": False, "database": database, "server": server,
                    "version": None, "message": "Connection failed", "error": message,
                    "db_type": db_type_lower}
        finally:
            if engine is not None:
                engine.dispose()


db_manager = DatabaseManager()

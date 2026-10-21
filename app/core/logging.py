import logging
import sys
from pathlib import Path
from app.core.config import settings


def setup_logging():
    """
    Configures structured logging for the application.
    Terminal output is disabled by default to keep the console clean.
    All application logs are safely routed to data/app.log.
    """
    log_format = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handlers = []
    # Log to file instead of terminal to keep console completely clean
    try:
        log_dir = Path(__file__).resolve().parent.parent.parent / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "app.log"
        handlers.append(logging.FileHandler(str(log_file), encoding="utf-8"))
    except Exception:
        pass

    # Console logging is only added if explicitly enabled in configuration
    if getattr(settings, "ENABLE_CONSOLE_LOGS", False):
        handlers.append(logging.StreamHandler(sys.stdout))
    elif not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=handlers,
        force=True
    )

    # Suppress verbose third-party loggers and remove any default console handlers
    noisy_loggers = [
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "fastapi",
        "sqlalchemy.engine",
        "excel_to_sql",
        "alembic"
    ]
    for name in noisy_loggers:
        lg = logging.getLogger(name)
        if not getattr(settings, "ENABLE_CONSOLE_LOGS", False):
            # Remove any console StreamHandlers attached by uvicorn/defaults
            lg.handlers = [h for h in lg.handlers if not isinstance(h, logging.StreamHandler)]
            lg.setLevel(logging.WARNING)


logger = logging.getLogger("excel_to_sql")

# import logging
# import sys
# from app.core.config import settings


# def setup_logging():
#     """
#     Configures structured logging for the application.
#     """
#     log_format = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
#     date_format = "%Y-%m-%d %H:%M:%S"

#     log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

#     logging.basicConfig(
#         level=log_level,
#         format=log_format,
#         datefmt=date_format,
#         handlers=[
#             logging.StreamHandler(sys.stdout)
#         ]
#     )

#     # Silence verbose third-party loggers if needed
#     logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# logger = logging.getLogger("excel_to_sql")
import logging
import sys
from app.core.config import settings


def setup_logging():
    """
    Configures structured logging for the application.
    """
    log_format = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Silence verbose third-party loggers if needed
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger("excel_to_sql")
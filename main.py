import sys
import time
from pathlib import Path

# Ensure the backend directory is on sys.path so imports use one canonical `app` package.
project_dir = Path(__file__).resolve().parent
if str(project_dir) not in sys.path:
    sys.path.insert(0, str(project_dir))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.logging import setup_logging, logger
from app.api import database_router, excel_router, migration_router, synonyms_router, auth_router

# Initialize logging
setup_logging()
logger.info(f"Starting Intelligent Excel-to-SQL backend in {settings.APP_ENV} mode")

app = FastAPI(
    title="Intelligent Excel-to-SQL API",
    description="Automatic Excel to Database Migration System",
    version="1.0.0"
)

# Request/Response Timing Middleware
@app.middleware("http")
async def add_timing_middleware(request: Request, call_next):
    t0 = time.time()
    logger.info(f"[API] REQUEST START | {request.method} {request.url.path}")
    response = await call_next(request)
    t1 = time.time()
    logger.info(f"[API] RESPONSE SENT | {request.method} {request.url.path} | Status={response.status_code} | {t1 - t0:.3f}s")
    return response

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(database_router)
app.include_router(excel_router)
app.include_router(migration_router)
app.include_router(synonyms_router)
app.include_router(auth_router)


@app.get("/")
def root():
    return {
        "app": "Intelligent Excel-to-SQL",
        "application": "Intelligent Excel-to-SQL",
        "status": "online",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    return {
        "status": "UP",
        "db_type": settings.DB_TYPE,
        "db_name": settings.DB_NAME
    }

@app.on_event("startup")
def on_startup():
    try:
        from app.services.file_tracking_service import file_tracking_service
        file_tracking_service.ensure_schema()
    except Exception as e:
        logger.warning(f"Could not verify file tracking schema on startup: {e}")

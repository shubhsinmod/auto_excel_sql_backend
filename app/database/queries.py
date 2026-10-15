"""
Reusable SQL queries and helper utilities for database execution.
"""
from typing import List, Dict, Any, Optional
from sqlalchemy import text, Engine, Table, MetaData
from sqlalchemy.orm import Session
from app.core.logging import logger


def execute_raw_sql(session: Session, sql_string: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Executes raw SQL query and returns results as a list of dictionaries.
    """
    params = params or {}
    logger.debug(f"Executing SQL: {sql_string} with params: {params}")
    result = session.execute(text(sql_string), params)
    if result.returns_rows:
        keys = result.keys()
        return [dict(zip(keys, row)) for row in result.fetchall()]
    return []
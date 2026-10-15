import pandas as pd
from typing import Tuple, Dict, Any, List
from app.core.logging import logger


def is_cell_empty(value: Any) -> bool:
    """
    Checks if a single cell value is empty according to requirements:
    - None
    - NaN or NaT
    - Empty string ""
    - Whitespace-only string (spaces, tabs, newlines, etc.)
    """
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        return value.strip() == ""

    return False


def is_column_completely_empty(series: pd.Series) -> bool:
    """
    Returns True if and only if EVERY data cell in the column is empty.
    If even one data cell contains a valid value, returns False.
    """
    if series.empty:
        return True

    # Fast-path check: if all values are isna, it's completely empty
    # For object/string columns, verify whitespace-only strings
    return bool(series.apply(is_cell_empty).all())


def remove_completely_empty_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Detect and remove completely empty columns from DataFrame.
    A column is removed ONLY if all data cells in that column are empty
    (None, NaN, NaT, empty string, or whitespace-only string).

    Preserves:
    - All rows and row order
    - Partially populated columns (even with just 1 valid cell)
    - Column order of remaining columns
    - Populated columns with missing/blank headers (reported in metadata)

    Returns:
    - cleaned_df: DataFrame with empty columns dropped
    - metadata: Detailed statistics and reporting dictionary
    """
    original_column_count = len(df.columns)
    removed_columns: List[Dict[str, Any]] = []
    columns_to_drop: List[Any] = []
    missing_header_columns: List[Dict[str, Any]] = []

    for idx, col in enumerate(df.columns):
        series = df[col]
        col_str = str(col).strip()
        is_blank_header = (
            col is None
            or col_str == ""
            or col_str.lower().startswith("unnamed:")
            or str(col).startswith("Column_")
        )

        if is_column_completely_empty(series):
            columns_to_drop.append(col)
            removed_columns.append({
                "name": str(col),
                "position": idx,
                "position_1_based": idx + 1
            })
        else:
            if is_blank_header:
                missing_header_columns.append({
                    "name": str(col),
                    "position": idx,
                    "position_1_based": idx + 1
                })

    if columns_to_drop:
        cleaned_df = df.drop(columns=columns_to_drop).copy()
    else:
        cleaned_df = df.copy()

    final_column_count = len(cleaned_df.columns)
    removed_column_count = len(removed_columns)

    metadata: Dict[str, Any] = {
        "original_column_count": original_column_count,
        "removed_column_count": removed_column_count,
        "removed_columns": removed_columns,
        "final_column_count": final_column_count,
        "missing_header_columns": missing_header_columns,
    }

    if removed_column_count > 0:
        logger.info(
            f"[CLEANER] Removed {removed_column_count} completely empty columns out of {original_column_count}. "
            f"Final columns: {final_column_count}. Removed details: {removed_columns}"
        )
    else:
        logger.info(
            f"[CLEANER] No completely empty columns found across {original_column_count} columns."
        )

    if missing_header_columns:
        logger.warning(
            f"[CLEANER] Detected {len(missing_header_columns)} populated columns with missing/unnamed headers: {missing_header_columns}"
        )

    return cleaned_df, metadata

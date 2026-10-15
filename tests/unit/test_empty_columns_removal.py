import os
import tempfile
import pytest
import pandas as pd
import numpy as np

from app.excel.cleaner import is_cell_empty, is_column_completely_empty, remove_completely_empty_columns
from app.excel.reader import load_file_dataframe
from app.excel.analyzer import analyze_excel_file


# =================================================================================================
# 1. UNIT TESTS: is_cell_empty
# =================================================================================================

def test_is_cell_empty_values():
    """Verify that None, NaN, NaT, empty string, and whitespace-only strings are considered empty."""
    assert is_cell_empty(None) is True
    assert is_cell_empty(np.nan) is True
    assert is_cell_empty(pd.NA) is True
    assert is_cell_empty(pd.NaT) is True
    assert is_cell_empty("") is True
    assert is_cell_empty("   ") is True
    assert is_cell_empty("\t") is True
    assert is_cell_empty("\n\r  \t") is True

    # Valid values should NOT be empty
    assert is_cell_empty(0) is False
    assert is_cell_empty(0.0) is False
    assert is_cell_empty(False) is False
    assert is_cell_empty("Valid Text") is False
    assert is_cell_empty("  Data  ") is False


# =================================================================================================
# 2. UNIT TESTS: remove_completely_empty_columns
# =================================================================================================

def test_single_completely_empty_column():
    """Verify dropping a single completely empty column while keeping populated columns."""
    df = pd.DataFrame({
        "ID": [1, 2, 3],
        "EmptyCol": [None, np.nan, ""],
        "Name": ["Alice", "Bob", "Charlie"]
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert list(cleaned_df.columns) == ["ID", "Name"]
    assert meta["original_column_count"] == 3
    assert meta["removed_column_count"] == 1
    assert meta["final_column_count"] == 2
    assert meta["removed_columns"][0]["name"] == "EmptyCol"
    assert meta["removed_columns"][0]["position"] == 1
    assert len(cleaned_df) == 3


def test_multiple_completely_empty_columns():
    """Verify dropping multiple completely empty columns at various positions."""
    df = pd.DataFrame({
        "Empty1": [None, None, None],
        "ColA": ["A1", "A2", "A3"],
        "Empty2": ["", "  ", "\t\n"],
        "ColB": [10, 20, 30],
        "Empty3": [pd.NA, np.nan, None]
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert list(cleaned_df.columns) == ["ColA", "ColB"]
    assert meta["original_column_count"] == 5
    assert meta["removed_column_count"] == 3
    assert meta["final_column_count"] == 2
    removed_names = [col["name"] for col in meta["removed_columns"]]
    assert removed_names == ["Empty1", "Empty2", "Empty3"]


def test_partially_populated_column_is_preserved():
    """
    Example from requirements:
    If a column contains 100 rows and 4 rows have empty values while the remaining 96 rows
    contain data, do NOT remove that column.
    """
    data = ["Val"] * 96 + [None, "", "   ", np.nan]
    df = pd.DataFrame({
        "PartialCol": data,
        "RowID": list(range(100))
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert "PartialCol" in cleaned_df.columns
    assert len(cleaned_df.columns) == 2
    assert meta["removed_column_count"] == 0
    assert len(cleaned_df) == 100


def test_column_with_whitespace_only_values():
    """Verify columns containing whitespace, tabs, and newlines are recognized as completely empty."""
    df = pd.DataFrame({
        "SpacesCol": ["   ", "  \t  ", "\n", "\r\n  \t"],
        "ValidCol": ["X", "Y", "Z", "W"]
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert list(cleaned_df.columns) == ["ValidCol"]
    assert meta["removed_column_count"] == 1
    assert meta["removed_columns"][0]["name"] == "SpacesCol"


def test_column_containing_valid_data_in_just_one_row():
    """Verify that if even ONE data cell contains a valid value, the entire column is preserved."""
    df = pd.DataFrame({
        "SingleValueCol": [None, np.nan, "Valid In Row 3", ""],
        "OtherCol": [1, 2, 3, 4]
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert "SingleValueCol" in cleaned_df.columns
    assert meta["removed_column_count"] == 0
    assert len(cleaned_df.columns) == 2
    assert cleaned_df["SingleValueCol"].iloc[2] == "Valid In Row 3"


def test_file_with_no_empty_columns():
    """Verify that when no empty columns exist, processing proceeds with no columns removed."""
    df = pd.DataFrame({
        "Col1": [1, 2, 3],
        "Col2": ["A", "B", "C"],
        "Col3": [10.5, 20.5, 30.5]
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert list(cleaned_df.columns) == ["Col1", "Col2", "Col3"]
    assert meta["original_column_count"] == 3
    assert meta["removed_column_count"] == 0
    assert meta["final_column_count"] == 3
    assert meta["removed_columns"] == []


def test_populated_column_with_blank_header_is_preserved_and_reported():
    """
    Verify that if a column has a blank or unnamed header but contains data:
    - It is preserved (not deleted)
    - It is reported in missing_header_columns metadata
    """
    df = pd.DataFrame({
        "Unnamed: 0": [100, 200, 300],
        "EmptyCol": [None, "", "   "],
        "Name": ["Alice", "Bob", "Charlie"]
    })

    cleaned_df, meta = remove_completely_empty_columns(df)

    assert "Unnamed: 0" in cleaned_df.columns
    assert "EmptyCol" not in cleaned_df.columns
    assert "Name" in cleaned_df.columns
    assert meta["removed_column_count"] == 1
    assert len(meta["missing_header_columns"]) == 1
    assert meta["missing_header_columns"][0]["name"] == "Unnamed: 0"


# =================================================================================================
# 3. INTEGRATION TESTS: CSV & EXCEL UPLOADS VIA load_file_dataframe & analyzer
# =================================================================================================

def test_csv_upload_drops_completely_empty_columns():
    """Verify CSV reading automatically removes completely empty columns."""
    csv_content = (
        "ID,Empty1,Name,Empty2,Amount\n"
        "1,,Alpha,  ,100.50\n"
        "2,,Beta, \t ,200.75\n"
        "3,,Gamma,   ,300.00\n"
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(csv_content)
        temp_path = f.name

    try:
        # 1. Test load_file_dataframe
        df = load_file_dataframe(temp_path)
        assert list(df.columns) == ["ID", "Name", "Amount"]
        assert len(df) == 3

        # 2. Test analyze_excel_file
        analysis = analyze_excel_file(temp_path, "test.csv", "upload_csv_test")
        assert analysis["columns"] == ["ID", "Name", "Amount"]
        assert analysis["columnCount"] == 3
        assert analysis["originalColumnCount"] == 5
        assert analysis["removedColumnCount"] == 2
        removed_col_names = [c["name"] for c in analysis["removedColumns"]]
        assert "Empty1" in removed_col_names
        assert "Empty2" in removed_col_names

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_excel_upload_drops_completely_empty_columns():
    """Verify Excel (.xlsx) upload automatically removes completely empty columns."""
    temp_path = os.path.join(tempfile.gettempdir(), "test_empty_cols.xlsx")

    raw_df = pd.DataFrame({
        "Order_ID": [101, 102, 103],
        "Empty_Middle": [None, np.nan, "   "],
        "Customer": ["John", "Jane", "Alice"],
        "Empty_End": [None, None, None]
    })
    raw_df.to_excel(temp_path, index=False)

    try:
        # 1. Test load_file_dataframe
        df = load_file_dataframe(temp_path)
        assert list(df.columns) == ["Order_ID", "Customer"]
        assert len(df) == 3

        # 2. Test analyze_excel_file
        analysis = analyze_excel_file(temp_path, "test_empty_cols.xlsx", "upload_excel_test")
        assert analysis["columns"] == ["Order_ID", "Customer"]
        assert analysis["columnCount"] == 2
        assert analysis["originalColumnCount"] == 4
        assert analysis["removedColumnCount"] == 2
        removed_names = [c["name"] for c in analysis["removedColumns"]]
        assert "Empty_Middle" in removed_names
        assert "Empty_End" in removed_names
        assert len(analysis["sampleRows"]) == 3
        assert "Empty_Middle" not in analysis["sampleRows"][0]

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# import os
# import pandas as pd
# import openpyxl
# from typing import List, Dict, Any, Optional
# from app.core.logging import logger


# def get_sheet_names(file_path: str) -> List[str]:
#     """
#     Returns list of worksheet/table names from the file.
#     """
#     ext = os.path.splitext(file_path)[1].lower()
#     if ext in (".xlsx", ".xlsm"):
#         try:
#             wb = openpyxl.load_workbook(file_path, read_only=True)
#             names = wb.sheetnames
#             wb.close()
#             return names
#         except Exception:
#             pass
#     elif ext in (".xls", ".xlsb"):
#         try:
#             xl = pd.ExcelFile(file_path)
#             return xl.sheet_names
#         except Exception:
#             pass
#     return ["Sheet1"]


# def load_file_dataframe(
#     file_path: str,
#     sheet_name: Optional[str] = None,
#     header: Optional[Any] = 0,
#     nrows: Optional[int] = None
# ) -> pd.DataFrame:
#     """
#     Reads any data file (Excel, CSV, TSV, JSON, TXT, Parquet, XML) into a pandas DataFrame.
#     Preserves raw values as string/object.
#     """
#     ext = os.path.splitext(file_path)[1].lower()

#     if ext in (".xlsx", ".xls", ".xlsm", ".xlsb"):
#         if sheet_name is None:
#             sheets = get_sheet_names(file_path)
#             sheet_name = sheets[0] if sheets else 0
#         return pd.read_excel(
#             file_path,
#             sheet_name=sheet_name,
#             header=header,
#             nrows=nrows,
#             dtype=object
#         )

#     elif ext in (".csv", ".txt"):
#         try:
#             # Let python engine auto-detect delimiter (comma, tab, semicolon, pipe)
#             return pd.read_csv(
#                 file_path,
#                 header=header,
#                 nrows=nrows,
#                 dtype=object,
#                 sep=None,
#                 engine="python",
#                 encoding="utf-8-sig"
#             )
#         except Exception:
#             return pd.read_csv(
#                 file_path,
#                 header=header,
#                 nrows=nrows,
#                 dtype=object,
#                 encoding="latin1"
#             )

#     elif ext == ".tsv":
#         return pd.read_csv(
#             file_path,
#             sep="\t",
#             header=header,
#             nrows=nrows,
#             dtype=object,
#             encoding="utf-8-sig"
#         )

#     elif ext in (".json", ".jsonl"):
#         try:
#             return pd.read_json(file_path, dtype=object)
#         except Exception:
#             return pd.read_json(file_path, lines=True, dtype=object)

#     elif ext == ".parquet":
#         return pd.read_parquet(file_path)

#     elif ext == ".xml":
#         return pd.read_xml(file_path)

#     else:
#         # Fallback: attempt CSV / delimited read, else read_excel
#         try:
#             return pd.read_csv(
#                 file_path,
#                 header=header,
#                 nrows=nrows,
#                 dtype=object,
#                 sep=None,
#                 engine="python"
#             )
#         except Exception:
#             return pd.read_excel(file_path, header=header, nrows=nrows, dtype=object)


# def load_excel_dataframe(
#     file_path: str,
#     sheet_name: Optional[str] = None,
#     nrows: Optional[int] = None
# ) -> pd.DataFrame:
#     """
#     Backward-compatible alias for load_file_dataframe.
#     """
#     return load_file_dataframe(file_path, sheet_name=sheet_name, nrows=nrows)
import os
import pandas as pd
import openpyxl
from typing import List, Dict, Any, Optional
from app.core.logging import logger


def get_sheet_names(file_path: str) -> List[str]:
    """
    Returns list of worksheet/table names from the file.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            wb = openpyxl.load_workbook(file_path, read_only=True)
            names = wb.sheetnames
            wb.close()
            return names
        except Exception:
            pass
    elif ext in (".xls", ".xlsb"):
        try:
            xl = pd.ExcelFile(file_path)
            return xl.sheet_names
        except Exception:
            pass
    return ["Sheet1"]


from app.excel.cleaner import remove_completely_empty_columns


def load_file_dataframe(
    file_path: str,
    sheet_name: Optional[str] = None,
    header: Optional[Any] = 0,
    nrows: Optional[int] = None,
    drop_empty_columns: bool = True
) -> pd.DataFrame:
    """
    Reads any data file (Excel, CSV, TSV, JSON, TXT, Parquet, XML) into a pandas DataFrame.
    Preserves raw values as string/object.
    When drop_empty_columns is True and header is not None, automatically removes completely empty columns.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in (".xlsx", ".xls", ".xlsm", ".xlsb"):
        if sheet_name is None:
            sheets = get_sheet_names(file_path)
            sheet_name = sheets[0] if sheets else 0
        df = pd.read_excel(
            file_path,
            sheet_name=sheet_name,
            header=header,
            nrows=nrows,
            dtype=object
        )

    elif ext in (".csv", ".txt"):
        try:
            # Let python engine auto-detect delimiter (comma, tab, semicolon, pipe)
            df = pd.read_csv(
                file_path,
                header=header,
                nrows=nrows,
                dtype=object,
                sep=None,
                engine="python",
                encoding="utf-8-sig"
            )
        except Exception:
            df = pd.read_csv(
                file_path,
                header=header,
                nrows=nrows,
                dtype=object,
                encoding="latin1"
            )

    elif ext == ".tsv":
        df = pd.read_csv(
            file_path,
            sep="\t",
            header=header,
            nrows=nrows,
            dtype=object,
            encoding="utf-8-sig"
        )

    elif ext in (".json", ".jsonl"):
        try:
            df = pd.read_json(file_path, dtype=object)
        except Exception:
            df = pd.read_json(file_path, lines=True, dtype=object)

    elif ext == ".parquet":
        df = pd.read_parquet(file_path)

    elif ext == ".xml":
        df = pd.read_xml(file_path)

    else:
        # Fallback: attempt CSV / delimited read, else read_excel
        try:
            df = pd.read_csv(
                file_path,
                header=header,
                nrows=nrows,
                dtype=object,
                sep=None,
                engine="python"
            )
        except Exception:
            df = pd.read_excel(file_path, header=header, nrows=nrows, dtype=object)

    if drop_empty_columns and header is not None and not df.empty:
        df, _ = remove_completely_empty_columns(df)

    return df


def load_excel_dataframe(
    file_path: str,
    sheet_name: Optional[str] = None,
    nrows: Optional[int] = None
) -> pd.DataFrame:
    """
    Backward-compatible alias for load_file_dataframe.
    """
    return load_file_dataframe(file_path, sheet_name=sheet_name, nrows=nrows)
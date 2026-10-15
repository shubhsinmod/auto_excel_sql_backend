# import os
# import math
# import datetime
# import time
# import openpyxl
# import pandas as pd
# from typing import Dict, Any, List, Optional, Tuple
# from app.core.logging import logger
# from app.excel.reader import load_file_dataframe, get_sheet_names


# def clean_cell_value(val: Any) -> Any:
#     """
#     Converts pandas / numpy data types, NaNs, and dates
#     to standard JSON-serializable Python values.
#     """
#     if val is None:
#         return None

#     try:
#         if pd.isna(val):
#             return None
#     except (TypeError, ValueError):
#         pass

#     if isinstance(val, (datetime.date, datetime.datetime)):
#         return val.isoformat()

#     if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
#         return None

#     return val if isinstance(val, (int, float, bool)) else str(val)


# def detect_header_row(
#     preview_buffer: List[Tuple[int, tuple]]
# ) -> Tuple[int, List[str], List[int], str]:
#     """
#     Dynamically analyzes initial non-empty rows using deterministic structural quality scoring.
#     Identifies whether Row 1, Row 2, Row 3... is the actual table header.
#     Returns: (header_row_number_1_based, headers_list, skipped_title_rows_1_based, method_name)
#     """
#     if not preview_buffer:
#         return 1, [], [], "DEFAULT"

#     # Determine maximum filled cell count across candidate rows
#     max_filled = max(
#         sum(1 for c in r if c is not None and str(c).strip() != "")
#         for _, r in preview_buffer
#     )
#     if max_filled == 0:
#         max_filled = 1

#     candidate_scores = []

#     for i, (row_num, row_vals) in enumerate(preview_buffer):
#         cleaned_cells = [str(c).strip() if c is not None else "" for c in row_vals]
#         non_empty = [c for c in cleaned_cells if c != ""]
#         filled_count = len(non_empty)

#         if filled_count == 0:
#             candidate_scores.append((row_num, -1000.0, cleaned_cells, i))
#             continue

#         # 1. Density Score (ratio of filled cells to maximum table width)
#         density = filled_count / max_filled

#         # 2. Comparison with subsequent rows' cell counts
#         next_rows = preview_buffer[i + 1 : i + 4]
#         next_filled_counts = [
#             sum(1 for c in r if c is not None and str(c).strip() != "")
#             for _, r in next_rows
#         ]
#         next_avg = sum(next_filled_counts) / len(next_filled_counts) if next_filled_counts else filled_count
#         density_match = filled_count / max(next_avg, 1.0)

#         # 3. Value Uniqueness Score
#         unique_count = len(set(non_empty))
#         uniqueness = unique_count / max(filled_count, 1)

#         # 4. Text characteristics
#         avg_char_len = sum(len(c) for c in non_empty) / max(filled_count, 1)
#         max_char_len = max(len(c) for c in non_empty)

#         is_single_cell_title = (filled_count == 1 and max_filled > 2) or (filled_count < max_filled * 0.4 and max_char_len > 35)
#         has_long_paragraph = max_char_len > 60

#         # Score composition
#         score = (
#             + 50.0 * density
#             + 25.0 * min(density_match, 1.2)
#             + 25.0 * uniqueness
#         )

#         if is_single_cell_title:
#             score -= 60.0
#         if has_long_paragraph:
#             score -= 40.0

#         candidate_scores.append((row_num, score, cleaned_cells, i))

#     # Pick candidate row with highest score
#     # In case of tie, prefer earlier row
#     sorted_candidates = sorted(candidate_scores, key=lambda x: (x[1], -x[0]), reverse=True)
#     best_row_num, best_score, raw_cols, best_buffer_idx = sorted_candidates[0]

#     # Generate clean, unique column header names
#     # Truncate headers to max_filled width to ignore trailing empty columns
#     max_non_empty_idx = 0
#     for idx, c in enumerate(raw_cols):
#         if c != "":
#             max_non_empty_idx = idx

#     raw_cols = raw_cols[: max_non_empty_idx + 1]

#     headers = []
#     seen = {}
#     for idx, col in enumerate(raw_cols):
#         clean_col = col if col and not col.startswith("Unnamed:") else f"Column_{idx + 1}"
#         if clean_col in seen:
#             seen[clean_col] += 1
#             headers.append(f"{clean_col}_{seen[clean_col]}")
#         else:
#             seen[clean_col] = 1
#             headers.append(clean_col)

#     # Collect 1-based row numbers of skipped title/metadata rows preceding the header
#     skipped_title_rows = [
#         r_num for r_num, s, _, _ in candidate_scores
#         if r_num < best_row_num and s > -500.0
#     ]
#     skipped_title_rows.sort()

#     return best_row_num, headers, skipped_title_rows, "STRUCTURAL_ANALYSIS"


# def analyze_excel_file(
#     file_path: str,
#     original_filename: str,
#     upload_id: str,
#     target_sheet: Optional[str] = None,
#     sample_size: int = 10
# ) -> Dict[str, Any]:

#     total_start = time.perf_counter()

#     logger.info(
#         f"[ANALYZER] START | file={original_filename} | "
#         f"id={upload_id}"
#     )

#     # ---------------------------------------------------------
#     # FILE INFORMATION
#     # ---------------------------------------------------------

#     start = time.perf_counter()

#     file_size_bytes = (
#         os.path.getsize(file_path)
#         if os.path.exists(file_path)
#         else 0
#     )

#     ext = os.path.splitext(file_path)[1].lower()

#     logger.info(
#         f"[ANALYZER] File information completed in "
#         f"{time.perf_counter() - start:.3f}s | "
#         f"size={file_size_bytes / 1024:.2f} KB | ext={ext}"
#     )

#     # ---------------------------------------------------------
#     # XLSX
#     # ---------------------------------------------------------

#     if ext == ".xlsx":

#         start = time.perf_counter()

#         logger.info("[ANALYZER] openpyxl.load_workbook START")

#         wb = openpyxl.load_workbook(
#             file_path,
#             read_only=True,
#             data_only=True
#         )

#         logger.info(
#             f"[ANALYZER] openpyxl.load_workbook END | "
#             f"{time.perf_counter() - start:.3f}s"
#         )

#         # -----------------------------------------------------
#         # SHEETS
#         # -----------------------------------------------------

#         start = time.perf_counter()

#         sheets = wb.sheetnames

#         sheet_name = (
#             target_sheet
#             if target_sheet and target_sheet in sheets
#             else sheets[0] if sheets else "Sheet1"
#         )

#         logger.info(
#             f"[ANALYZER] Sheet discovery completed in "
#             f"{time.perf_counter() - start:.3f}s | "
#             f"sheets={sheets} | selected={sheet_name}"
#         )

#         # -----------------------------------------------------
#         # WORKSHEET
#         # -----------------------------------------------------

#         start = time.perf_counter()

#         ws = wb[sheet_name]

#         logger.info(
#             f"[ANALYZER] Worksheet loaded in "
#             f"{time.perf_counter() - start:.3f}s"
#         )

#         logger.info(
#             f"[ANALYZER] Worksheet dimensions | "
#             f"max_row={ws.max_row} | "
#             f"max_column={ws.max_column}"
#         )

#         # -----------------------------------------------------
#         # DYNAMIC HEADER DETECTION (XLSX)
#         # -----------------------------------------------------

#         start = time.perf_counter()
#         row_iterator = ws.iter_rows(values_only=True)

#         preview_buffer: List[Tuple[int, tuple]] = []
#         current_row_idx = 0

#         # Read top candidate rows (up to 20 non-empty rows) for structural header detection
#         for row in row_iterator:
#             current_row_idx += 1
#             if any(c is not None and str(c).strip() != "" for c in row):
#                 preview_buffer.append((current_row_idx, row))
#                 if len(preview_buffer) >= 20:
#                     break

#         header_row_num, headers, skipped_title_rows, detection_method = detect_header_row(preview_buffer)

#         logger.info(
#             f"[ANALYZER] Header detection completed in "
#             f"{time.perf_counter() - start:.3f}s | "
#             f"headerRow={header_row_num} | "
#             f"skippedTitles={skipped_title_rows} | "
#             f"columns={len(headers)}"
#         )

#         # -----------------------------------------------------
#         # DATA ROWS ITERATION (XLSX)
#         # -----------------------------------------------------

#         sample_rows: List[Dict[str, Any]] = []
#         row_count = 0

#         if headers:
#             start = time.perf_counter()
#             processed_rows = 0

#             # 1. Process remaining buffered rows after header_row_num
#             for r_idx, r_vals in preview_buffer:
#                 if r_idx > header_row_num:
#                     row_count += 1
#                     processed_rows += 1

#                     if len(sample_rows) < sample_size:
#                         row_dict = {
#                             col_name: clean_cell_value(val)
#                             for col_name, val in zip(headers, r_vals)
#                         }
#                         sample_rows.append(row_dict)

#             # 2. Process rest of worksheet rows from row_iterator
#             for row in row_iterator:
#                 current_row_idx += 1
#                 if not any(c is not None and str(c).strip() != "" for c in row):
#                     continue

#                 row_count += 1
#                 processed_rows += 1

#                 if len(sample_rows) < sample_size:
#                     row_dict = {
#                         col_name: clean_cell_value(val)
#                         for col_name, val in zip(headers, row)
#                     }
#                     sample_rows.append(row_dict)

#                 if processed_rows % 1000 == 0:
#                     logger.info(f"[ANALYZER] Processed {processed_rows} data rows in {time.perf_counter() - start:.3f}s")

#             logger.info(
#                 f"[ANALYZER] Data row iteration END | "
#                 f"dataRows={row_count} | "
#                 f"time={time.perf_counter() - start:.3f}s"
#             )

#         wb.close()

#     # ---------------------------------------------------------
#     # Other formats: XLS, CSV, TSV, JSON, TXT, Parquet, XML
#     # ---------------------------------------------------------

#     else:

#         start = time.perf_counter()
#         sheets = get_sheet_names(file_path)

#         sheet_name = (
#             target_sheet
#             if target_sheet and target_sheet in sheets
#             else sheets[0] if sheets else "Default"
#         )

#         df_raw = load_file_dataframe(file_path, sheet_name=sheet_name, header=None)

#         preview_buffer: List[Tuple[int, tuple]] = []
#         for orig_idx, r in df_raw.iterrows():
#             row_vals = tuple(r.values)
#             if any(c is not None and not pd.isna(c) and str(c).strip() != "" for c in row_vals):
#                 preview_buffer.append((orig_idx + 1, row_vals))
#                 if len(preview_buffer) >= 20:
#                     break

#         header_row_num, headers, skipped_title_rows, detection_method = detect_header_row(preview_buffer)

#         sample_rows = []
#         row_count = 0

#         for orig_idx, r in df_raw.iterrows():
#             r_num = orig_idx + 1
#             row_vals = tuple(r.values)

#             if r_num <= header_row_num:
#                 continue
#             if not any(c is not None and not pd.isna(c) and str(c).strip() != "" for c in row_vals):
#                 continue

#             row_count += 1
#             if len(sample_rows) < sample_size:
#                 r_dict = {
#                     col_name: clean_cell_value(v)
#                     for col_name, v in zip(headers, row_vals)
#                 }
#                 sample_rows.append(r_dict)

#     # ---------------------------------------------------------
#     # FINAL METADATA
#     # ---------------------------------------------------------

#     total_time = time.perf_counter() - total_start

#     logger.info(
#         f"[ANALYZER] COMPLETE | "
#         f"headerRow={header_row_num} | "
#         f"dataRows={row_count} | "
#         f"columns={len(headers)} | "
#         f"total_time={total_time:.3f}s"
#     )

#     return {
#         "uploadId": upload_id,
#         "fileName": original_filename,
#         "sheetName": sheet_name,
#         "availableSheets": sheets,
#         "rowCount": row_count,
#         "columnCount": len(headers),
#         "columns": headers,
#         "sampleRows": sample_rows,
#         "fileSizeBytes": file_size_bytes,
#         "createdAt": datetime.datetime.now().isoformat(),
#         "headerRowNumber": header_row_num,
#         "titleRowsSkipped": skipped_title_rows,
#         "headerDetectionMethod": detection_method
#     }
import os
import math
import datetime
import time
import openpyxl
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple
from app.core.logging import logger
from app.excel.reader import load_file_dataframe, get_sheet_names
from app.excel.cleaner import remove_completely_empty_columns, is_cell_empty


def clean_cell_value(val: Any) -> Any:
    """
    Converts pandas / numpy data types, NaNs, and dates
    to standard JSON-serializable Python values.
    """
    if val is None:
        return None

    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.isoformat()

    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None

    return val if isinstance(val, (int, float, bool)) else str(val)


def detect_header_row(
    preview_buffer: List[Tuple[int, tuple]]
) -> Tuple[int, List[str], List[int], str]:
    """
    Dynamically analyzes initial non-empty rows using deterministic structural quality scoring.
    Identifies whether Row 1, Row 2, Row 3... is the actual table header.
    Returns: (header_row_number_1_based, headers_list, skipped_title_rows_1_based, method_name)
    """
    if not preview_buffer:
        return 1, [], [], "DEFAULT"

    # Determine maximum filled cell count across candidate rows
    max_filled = max(
        sum(1 for c in r if c is not None and str(c).strip() != "")
        for _, r in preview_buffer
    )
    if max_filled == 0:
        max_filled = 1

    candidate_scores = []

    for i, (row_num, row_vals) in enumerate(preview_buffer):
        cleaned_cells = [str(c).strip() if c is not None else "" for c in row_vals]
        non_empty = [c for c in cleaned_cells if c != ""]
        filled_count = len(non_empty)

        if filled_count == 0:
            candidate_scores.append((row_num, -1000.0, cleaned_cells, i))
            continue

        # 1. Density Score (ratio of filled cells to maximum table width)
        density = filled_count / max_filled

        # 2. Comparison with subsequent rows' cell counts
        next_rows = preview_buffer[i + 1 : i + 4]
        next_filled_counts = [
            sum(1 for c in r if c is not None and str(c).strip() != "")
            for _, r in next_rows
        ]
        next_avg = sum(next_filled_counts) / len(next_filled_counts) if next_filled_counts else filled_count
        density_match = filled_count / max(next_avg, 1.0)

        # 3. Value Uniqueness Score
        unique_count = len(set(non_empty))
        uniqueness = unique_count / max(filled_count, 1)

        # 4. Text characteristics
        avg_char_len = sum(len(c) for c in non_empty) / max(filled_count, 1)
        max_char_len = max(len(c) for c in non_empty)

        is_single_cell_title = (filled_count == 1 and max_filled > 2) or (filled_count < max_filled * 0.4 and max_char_len > 35)
        has_long_paragraph = max_char_len > 60

        # Score composition
        score = (
            + 50.0 * density
            + 25.0 * min(density_match, 1.2)
            + 25.0 * uniqueness
        )

        if is_single_cell_title:
            score -= 60.0
        if has_long_paragraph:
            score -= 40.0

        candidate_scores.append((row_num, score, cleaned_cells, i))

    # Pick candidate row with highest score
    # In case of tie, prefer earlier row
    sorted_candidates = sorted(candidate_scores, key=lambda x: (x[1], -x[0]), reverse=True)
    best_row_num, best_score, raw_cols, best_buffer_idx = sorted_candidates[0]

    # Generate clean, unique column header names
    # Truncate headers to max_filled width to ignore trailing empty columns
    max_non_empty_idx = 0
    for idx, c in enumerate(raw_cols):
        if c != "":
            max_non_empty_idx = idx

    raw_cols = raw_cols[: max_non_empty_idx + 1]

    headers = []
    seen = {}
    for idx, col in enumerate(raw_cols):
        clean_col = col if col and not col.startswith("Unnamed:") else f"Column_{idx + 1}"
        if clean_col in seen:
            seen[clean_col] += 1
            headers.append(f"{clean_col}_{seen[clean_col]}")
        else:
            seen[clean_col] = 1
            headers.append(clean_col)

    # Collect 1-based row numbers of skipped title/metadata rows preceding the header
    skipped_title_rows = [
        r_num for r_num, s, _, _ in candidate_scores
        if r_num < best_row_num and s > -500.0
    ]
    skipped_title_rows.sort()

    return best_row_num, headers, skipped_title_rows, "STRUCTURAL_ANALYSIS"


def analyze_excel_file(
    file_path: str,
    original_filename: str,
    upload_id: str,
    target_sheet: Optional[str] = None,
    sample_size: int = 10
) -> Dict[str, Any]:

    total_start = time.perf_counter()

    logger.info(
        f"[ANALYZER] START | file={original_filename} | "
        f"id={upload_id}"
    )

    # ---------------------------------------------------------
    # FILE INFORMATION
    # ---------------------------------------------------------

    start = time.perf_counter()

    file_size_bytes = (
        os.path.getsize(file_path)
        if os.path.exists(file_path)
        else 0
    )

    ext = os.path.splitext(file_path)[1].lower()

    logger.info(
        f"[ANALYZER] File information completed in "
        f"{time.perf_counter() - start:.3f}s | "
        f"size={file_size_bytes / 1024:.2f} KB | ext={ext}"
    )

    # ---------------------------------------------------------
    # XLSX
    # ---------------------------------------------------------

    if ext == ".xlsx":

        start = time.perf_counter()

        logger.info("[ANALYZER] openpyxl.load_workbook START")

        wb = openpyxl.load_workbook(
            file_path,
            read_only=True,
            data_only=True
        )

        logger.info(
            f"[ANALYZER] openpyxl.load_workbook END | "
            f"{time.perf_counter() - start:.3f}s"
        )

        # -----------------------------------------------------
        # SHEETS
        # -----------------------------------------------------

        start = time.perf_counter()

        sheets = wb.sheetnames

        sheet_name = (
            target_sheet
            if target_sheet and target_sheet in sheets
            else sheets[0] if sheets else "Sheet1"
        )

        logger.info(
            f"[ANALYZER] Sheet discovery completed in "
            f"{time.perf_counter() - start:.3f}s | "
            f"sheets={sheets} | selected={sheet_name}"
        )

        # -----------------------------------------------------
        # WORKSHEET
        # -----------------------------------------------------

        start = time.perf_counter()

        ws = wb[sheet_name]

        logger.info(
            f"[ANALYZER] Worksheet loaded in "
            f"{time.perf_counter() - start:.3f}s"
        )

        logger.info(
            f"[ANALYZER] Worksheet dimensions | "
            f"max_row={ws.max_row} | "
            f"max_column={ws.max_column}"
        )

        # -----------------------------------------------------
        # DYNAMIC HEADER DETECTION (XLSX)
        # -----------------------------------------------------

        start = time.perf_counter()
        row_iterator = ws.iter_rows(values_only=True)

        preview_buffer: List[Tuple[int, tuple]] = []
        current_row_idx = 0

        # Read top candidate rows (up to 20 non-empty rows) for structural header detection
        for row in row_iterator:
            current_row_idx += 1
            if any(c is not None and str(c).strip() != "" for c in row):
                preview_buffer.append((current_row_idx, row))
                if len(preview_buffer) >= 20:
                    break

        header_row_num, headers, skipped_title_rows, detection_method = detect_header_row(preview_buffer)

        logger.info(
            f"[ANALYZER] Header detection completed in "
            f"{time.perf_counter() - start:.3f}s | "
            f"headerRow={header_row_num} | "
            f"skippedTitles={skipped_title_rows} | "
            f"columns={len(headers)}"
        )

        # -----------------------------------------------------
        # DATA ROWS ITERATION (XLSX)
        # -----------------------------------------------------

        sample_rows: List[Dict[str, Any]] = []
        row_count = 0
        remaining_rows_list: List[tuple] = []

        if headers:
            start = time.perf_counter()
            processed_rows = 0

            # 1. Process remaining buffered rows after header_row_num
            for r_idx, r_vals in preview_buffer:
                if r_idx > header_row_num:
                    row_count += 1
                    processed_rows += 1

                    if len(sample_rows) < sample_size:
                        row_dict = {
                            col_name: clean_cell_value(val)
                            for col_name, val in zip(headers, r_vals)
                        }
                        sample_rows.append(row_dict)

            # 2. Process rest of worksheet rows from row_iterator
            for row in row_iterator:
                current_row_idx += 1
                if not any(c is not None and str(c).strip() != "" for c in row):
                    continue

                remaining_rows_list.append(row)
                row_count += 1
                processed_rows += 1

                if len(sample_rows) < sample_size:
                    row_dict = {
                        col_name: clean_cell_value(val)
                        for col_name, val in zip(headers, row)
                    }
                    sample_rows.append(row_dict)

                if processed_rows % 1000 == 0:
                    logger.info(f"[ANALYZER] Processed {processed_rows} data rows in {time.perf_counter() - start:.3f}s")

            logger.info(
                f"[ANALYZER] Data row iteration END | "
                f"dataRows={row_count} | "
                f"time={time.perf_counter() - start:.3f}s"
            )

        wb.close()

    # ---------------------------------------------------------
    # Other formats: XLS, CSV, TSV, JSON, TXT, Parquet, XML
    # ---------------------------------------------------------

    else:

        start = time.perf_counter()
        sheets = get_sheet_names(file_path)

        sheet_name = (
            target_sheet
            if target_sheet and target_sheet in sheets
            else sheets[0] if sheets else "Default"
        )

        df_raw = load_file_dataframe(file_path, sheet_name=sheet_name, header=None)

        preview_buffer: List[Tuple[int, tuple]] = []
        for orig_idx, r in df_raw.iterrows():
            row_vals = tuple(r.values)
            if any(c is not None and not pd.isna(c) and str(c).strip() != "" for c in row_vals):
                preview_buffer.append((orig_idx + 1, row_vals))
                if len(preview_buffer) >= 20:
                    break

        header_row_num, headers, skipped_title_rows, detection_method = detect_header_row(preview_buffer)

        sample_rows = []
        row_count = 0

        for orig_idx, r in df_raw.iterrows():
            r_num = orig_idx + 1
            row_vals = tuple(r.values)

            if r_num <= header_row_num:
                continue
            if not any(c is not None and not pd.isna(c) and str(c).strip() != "" for c in row_vals):
                continue

            row_count += 1
            if len(sample_rows) < sample_size:
                r_dict = {
                    col_name: clean_cell_value(v)
                    for col_name, v in zip(headers, row_vals)
                }
                sample_rows.append(r_dict)

    # ---------------------------------------------------------
    # FILTER COMPLETELY EMPTY COLUMNS DYNAMICALLY
    # ---------------------------------------------------------
    empty_col_indices = set()
    missing_header_cols = []
    num_cols = len(headers)

    for c_idx in range(num_cols):
        col_name = headers[c_idx]
        col_str = str(col_name).strip()
        is_blank_header = (
            not col_name
            or col_str == ""
            or col_str.lower().startswith("unnamed:")
            or col_str.startswith("Column_")
        )

        has_data = False
        if ext == ".xlsx":
            # Check buffered rows after header
            for r_idx, r_vals in preview_buffer:
                if r_idx > header_row_num:
                    val = r_vals[c_idx] if c_idx < len(r_vals) else None
                    if not is_cell_empty(val):
                        has_data = True
                        break
            # Check remaining rows from worksheet
            if not has_data:
                for row_tuple in remaining_rows_list:
                    val = row_tuple[c_idx] if c_idx < len(row_tuple) else None
                    if not is_cell_empty(val):
                        has_data = True
                        break
        else:
            # Check all data rows from df_raw
            for orig_idx, r in df_raw.iterrows():
                if orig_idx + 1 > header_row_num:
                    val = r.iloc[c_idx] if c_idx < len(r) else None
                    if not is_cell_empty(val):
                        has_data = True
                        break

        if not has_data:
            empty_col_indices.add(c_idx)
        else:
            if is_blank_header:
                missing_header_cols.append({
                    "name": col_name,
                    "position": c_idx,
                    "position_1_based": c_idx + 1
                })

    original_column_count = num_cols
    removed_columns_meta = [
        {
            "name": headers[i],
            "position": i,
            "position_1_based": i + 1
        }
        for i in sorted(empty_col_indices)
    ]
    removed_column_count = len(removed_columns_meta)

    # Filter headers
    cleaned_headers = [h for i, h in enumerate(headers) if i not in empty_col_indices]

    # Filter sample rows
    cleaned_sample_rows = []
    for r in sample_rows:
        cleaned_sample_rows.append({
            col: r[col] for col in cleaned_headers if col in r
        })

    # ---------------------------------------------------------
    # FINAL METADATA
    # ---------------------------------------------------------

    total_time = time.perf_counter() - total_start

    logger.info(
        f"[ANALYZER] COMPLETE | "
        f"headerRow={header_row_num} | "
        f"dataRows={row_count} | "
        f"originalColumns={original_column_count} | "
        f"removedEmptyColumns={removed_column_count} | "
        f"finalColumns={len(cleaned_headers)} | "
        f"total_time={total_time:.3f}s"
    )

    return {
        "uploadId": upload_id,
        "fileName": original_filename,
        "sheetName": sheet_name,
        "availableSheets": sheets,
        "rowCount": row_count,
        "columnCount": len(cleaned_headers),
        "columns": cleaned_headers,
        "sampleRows": cleaned_sample_rows,
        "fileSizeBytes": file_size_bytes,
        "createdAt": datetime.datetime.now().isoformat(),
        "headerRowNumber": header_row_num,
        "titleRowsSkipped": skipped_title_rows,
        "headerDetectionMethod": detection_method,
        "originalColumnCount": original_column_count,
        "removedColumnCount": removed_column_count,
        "removedColumns": removed_columns_meta,
        "missingHeaderColumns": missing_header_cols
    }
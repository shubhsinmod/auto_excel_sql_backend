import re
import datetime
from typing import List, Any, Tuple, Optional


def normalize_column_name(s: str) -> str:
    """
    Normalizes column names across naming conventions into a canonical space-separated token string.
    Handles:
    - Lowercase: customer_name -> 'customer name'
    - Uppercase: CUSTOMER_NAME -> 'customer name'
    - PascalCase: CustomerName -> 'customer name'
    - camelCase: customerName -> 'customer name'
    - Spaces: Customer Name -> 'customer name'
    - Underscores: customer_name -> 'customer name'
    - Hyphens: customer-name -> 'customer name'
    - Repeated spaces and separators: customer   name, customer___name -> 'customer name'
    - Acronyms & abbreviations: CustomerID, customerID, customer_id -> 'customer id'
      orderURL, IPAddress, XMLParser -> 'order url', 'ip address', 'xml parser'
      bill_no, BillNo -> 'bill no'
      item_qty, ItemQty -> 'item qty'
    - Leading and trailing whitespace
    """
    if not s:
        return ""
    text = str(s).strip()
    if not text:
        return ""

    # Replace hyphens, underscores, dots, slashes, and symbols with spaces
    text = re.sub(r'[\s_\-\.\/\\#%]+', ' ', text)

    # Acronym followed by PascalCase word (e.g. 'XMLParser' -> 'XML Parser', 'IPAddress' -> 'IP Address')
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)

    # Lowercase/digit followed by Uppercase (e.g. 'customerName' -> 'customer Name', 'customerID' -> 'customer ID')
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)

    # Remove any remaining non-alphanumeric except space
    text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)

    # Collapse repeated whitespace and lowercase
    tokens = [t.lower() for t in text.split() if t]
    return " ".join(tokens)


def tokenize_column_name(s: str) -> List[str]:
    """Tokenizes a column name into lowercase word tokens."""
    return normalize_column_name(s).split()


def normalize_string(s: str) -> str:
    """Compact alphanumeric normalization (backwards-compatibility helper)."""
    if not s:
        return ""
    return "".join(tokenize_column_name(s))


def normalize_identifier(name: str) -> str:
    """
    Normalize table/column names for matching (e.g. 'Customer Name' -> 'customername').
    Maintained for backwards-compatibility.
    """
    if not name:
        return ""
    cleaned = re.sub(r"[_\-\s\W]+", "", name)
    return cleaned.lower()


def stem_token(t: str) -> str:
    """Simple morphological stemming for English database identifiers."""
    t = t.lower()
    if len(t) <= 3:
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("es") and len(t) > 3 and not t.endswith(("ss", "us", "is")):
        return t[:-2]
    if t.endswith("s") and len(t) > 3 and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def normalize_cell_value(val: Any) -> str:
    """
    Normalizes Excel/CSV cell values across naming conventions and formatting styles
    into a canonical space-separated lowercase token string.
    Handles:
    - Capitalization: 'TAKEAWAY', 'TakeAway', 'takeaway' -> 'take away'
    - Spaces: 'Take Away' -> 'take away'
    - Underscores: 'take_away' -> 'take away'
    - Hyphens: 'take-away', 'Take-Away' -> 'take away'
    - Punctuation & Parentheses: 'Delivery(Parcel)' -> 'delivery parcel', 'Dine-In.' -> 'dine in'
    - PascalCase/camelCase splits: 'HomeDelivery' -> 'home delivery', 'DineIn' -> 'dine in'
    """
    if val is None:
        return ""
    text = str(val).strip()
    if not text or text.lower() in ("nan", "none", "null", "nat"):
        return ""

    # Split punctuation, parentheses, brackets, hyphens, underscores, slashes, commas, dots
    text = re.sub(r'[\(\)\[\]\{\}\<\>_\-\.\,\;:\/\\#%!\?\*]+', ' ', text)

    # Acronym followed by PascalCase word (e.g. 'VIPOrder' -> 'VIP Order')
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)

    # Lowercase/digit followed by Uppercase (e.g. 'TakeAway' -> 'Take Away', 'HomeDelivery' -> 'Home Delivery')
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)

    # Remove any remaining non-alphanumeric except space
    text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)

    # Collapse repeated whitespace and lowercase
    tokens = [t.lower() for t in text.split() if t]
    return " ".join(tokens)


def tokenize_cell_value(val: Any) -> List[str]:
    """Tokenizes a cell value into lowercase normalized word tokens."""
    return normalize_cell_value(val).split()


def normalize_date_value(val: Any, target_type: str = "DATETIME") -> Tuple[Optional[str], Optional[datetime.datetime], bool]:
    """
    Normalizes Excel/CSV date values into a single consistent ISO format (and datetime object).
    Supports:
    - python datetime / date objects
    - pandas Timestamp
    - Excel serial numbers (e.g. 45000, 45000.5)
    - ISO strings (YYYY-MM-DD, YYYY-MM-DD HH:MM:SS)
    - Common string formats (DD/MM/YYYY, DD-MM-YYYY, MM/DD/YYYY)
    
    Returns:
        (formatted_str, dt_obj, is_valid)
        - If target_type is 'DATE', returns 'YYYY-MM-DD'
        - If target_type is 'DATETIME' or similar, returns 'YYYY-MM-DD HH:MM:SS'
        - If empty or None, returns (None, None, True)
        - If invalid/unparseable, returns (original_str, None, False)
    """
    import datetime
    import pandas as pd

    if val is None:
        return None, None, True

    # Check for pandas/numpy NA
    try:
        if pd.isna(val):
            return None, None, True
    except Exception:
        pass

    # If already a datetime or date object
    if isinstance(val, (datetime.datetime, pd.Timestamp)):
        dt = val.to_pydatetime() if isinstance(val, pd.Timestamp) else val
        is_date_only = "DATE" == target_type.upper()
        fmt = "%Y-%m-%d" if is_date_only else "%Y-%m-%d %H:%M:%S"
        return dt.strftime(fmt), dt, True
    elif isinstance(val, datetime.date):
        dt = datetime.datetime.combine(val, datetime.time.min)
        is_date_only = "DATE" == target_type.upper()
        fmt = "%Y-%m-%d" if is_date_only else "%Y-%m-%d %H:%M:%S"
        return dt.strftime(fmt), dt, True

    # Numeric Excel serial date (e.g., 45000 or 45000.5)
    if isinstance(val, (int, float)):
        try:
            # Excel base date is 1899-12-30
            dt = pd.to_datetime(val, unit='D', origin='1899-12-30').to_pydatetime()
            is_date_only = "DATE" == target_type.upper()
            fmt = "%Y-%m-%d" if is_date_only else "%Y-%m-%d %H:%M:%S"
            return dt.strftime(fmt), dt, True
        except Exception:
            return str(val), None, False

    str_val = str(val).strip()
    if not str_val or str_val.lower() in ("nan", "none", "null", "nat"):
        return None, None, True

    # Check if numeric string serial date (e.g. "45000")
    try:
        num_val = float(str_val)
        if 1000 <= num_val <= 100000:
            dt = pd.to_datetime(num_val, unit='D', origin='1899-12-30').to_pydatetime()
            is_date_only = "DATE" == target_type.upper()
            fmt = "%Y-%m-%d" if is_date_only else "%Y-%m-%d %H:%M:%S"
            return dt.strftime(fmt), dt, True
    except (ValueError, TypeError):
        pass

    # Parse string date: try standard/ISO format first without dayfirst to preserve YYYY-MM-DD
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # If string starts with 4-digit year (ISO format), use dayfirst=False
            if re.match(r'^\d{4}[-/]', str_val):
                dt_parsed = pd.to_datetime(str_val, errors='coerce', dayfirst=False)
            else:
                dt_parsed = pd.to_datetime(str_val, errors='coerce', dayfirst=True)

            if pd.isna(dt_parsed):
                dt_parsed = pd.to_datetime(str_val, errors='coerce')

        if pd.isna(dt_parsed):
            return str_val, None, False

        dt = dt_parsed.to_pydatetime()
        is_date_only = "DATE" == target_type.upper()
        fmt = "%Y-%m-%d" if is_date_only else "%Y-%m-%d %H:%M:%S"
        return dt.strftime(fmt), dt, True
    except Exception:
        return str_val, None, False


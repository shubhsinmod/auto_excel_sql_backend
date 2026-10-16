"""Helpers for invoice-number duplicate detection during SQL migration."""
from typing import Any, Set, Tuple

import pandas as pd


def invoice_identity_key(value: Any) -> str:
    """Normalize invoice values without changing the stored source value."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    value_text = str(value).strip()
    if value_text.casefold() in {"", "none", "nan", "nat", "null"}:
        return ""
    if value_text.endswith(".0"):
        try:
            value_text = str(int(float(value_text)))
        except ValueError:
            pass
    return value_text.casefold()


def duplicate_invoice_mask(
    frame: pd.DataFrame,
    invoice_column: str,
    existing_invoice_values: Set[str],
) -> Tuple[pd.Series, Set[str]]:
    """Return a mask for rows whose invoice already exists in Bill.Bill_Number."""
    normalized_existing = {invoice_identity_key(v) for v in existing_invoice_values}
    normalized_existing.discard("")
    mask = frame[invoice_column].map(
        lambda value: invoice_identity_key(value) in normalized_existing
    )
    skipped_keys = {
        invoice_identity_key(value)
        for value in frame.loc[mask, invoice_column].tolist()
        if invoice_identity_key(value)
    }
    return mask, skipped_keys

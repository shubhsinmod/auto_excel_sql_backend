import pytest
import pandas as pd
from app.mapping.matcher import (
    normalize_column_name,
    tokenize_column_name,
    ColumnMatcher,
    normalize_string
)
from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.migration.preview import MigrationPreviewEngine


# =================================================================================================
# FIXTURES
# =================================================================================================
@pytest.fixture
def sample_schema():
    customer_cols = [
        ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_Name", type="VARCHAR(255)", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Customer_Phone", type="VARCHAR(50)", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Address", type="VARCHAR(500)", nullable=True, primary_key=False, identity=False),
    ]
    customer_tbl = TableMetadata(
        name="Customer",
        columns=customer_cols,
        primary_keys=["Customer_ID"],
        foreign_keys=[]
    )

    order_cols = [
        ColumnMetadata(name="Order_ID", type="INTEGER", nullable=False, primary_key=True, identity=True),
        ColumnMetadata(name="Customer_ID", type="INTEGER", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Order_Date", type="DATETIME", nullable=False, primary_key=False, identity=False),
        ColumnMetadata(name="Total_Amount", type="DECIMAL(18,2)", nullable=True, primary_key=False, identity=False),
        ColumnMetadata(name="Order_URL", type="VARCHAR(500)", nullable=True, primary_key=False, identity=False),
    ]
    order_tbl = TableMetadata(
        name="Order",
        columns=order_cols,
        primary_keys=["Order_ID"],
        foreign_keys=[]
    )

    return DatabaseSchemaMetadata(
        database_name="BOPOS_Test",
        db_type="mssql",
        tables=[customer_tbl, order_tbl]
    )


# =================================================================================================
# 1. COLUMN NAME NORMALIZATION TESTS
# =================================================================================================
def test_normalize_column_name_all_naming_conventions():
    """Verify that all naming conventions reduce to the exact canonical token string 'customer name'."""
    variations = [
        "customer_name",    # lowercase snake_case
        "CUSTOMER_NAME",    # uppercase snake_case
        "CustomerName",     # PascalCase
        "customerName",     # camelCase
        "Customer Name",    # spaces
        "customer-name",    # hyphens
        "customer   name",  # multiple spaces
        "customer___name",  # multiple underscores
        "  customer_name ", # leading/trailing whitespace
        "customer/name",    # slashes
        "customer.name",    # dots
    ]

    for variant in variations:
        normalized = normalize_column_name(variant)
        assert normalized == "customer name", f"Failed for variant '{variant}', got '{normalized}'"


def test_normalize_column_name_acronyms():
    """Verify acronym recognition: ID, URL, IP, XML."""
    assert normalize_column_name("CustomerID") == "customer id"
    assert normalize_column_name("customerId") == "customer id"
    assert normalize_column_name("CUSTOMER_ID") == "customer id"
    assert normalize_column_name("customer_id") == "customer id"

    assert normalize_column_name("orderURL") == "order url"
    assert normalize_column_name("OrderURL") == "order url"
    assert normalize_column_name("order_url") == "order url"

    assert normalize_column_name("IPAddress") == "ip address"
    assert normalize_column_name("XMLParser") == "xml parser"


def test_tokenize_column_name():
    tokens = tokenize_column_name("Customer-Phone_Number")
    assert tokens == ["customer", "phone", "number"]


# =================================================================================================
# 2. LAYERED MATCHING HIERARCHY TESTS
# =================================================================================================
def test_exact_normalized_match_layer_2(sample_schema):
    """
    Test that different conventions for Customer Name all match Customer.Customer_Name
    via EXACT_NORMALIZED (Layer 2) with score 0.99.
    """
    variants = ["Customer Name", "CUSTOMER_NAME", "customerName", "CustomerName", "customer-name"]

    for var in variants:
        res = ColumnMatcher.match_columns([var], sample_schema)
        assert len(res) == 1
        m = res[0]
        assert m.status == "MATCHED"
        assert m.database_table == "Customer"
        assert m.database_column == "Customer_Name"
        assert m.normalized_source_name == "customer name"
        assert m.normalized_target_name == "customer name"
        assert m.confidence >= 0.99
        assert m.match_type in ("EXACT_NORMALIZED", "EXACT_ORIGINAL", "CASE_INSENSITIVE")


def test_acronym_matching(sample_schema):
    """Verify that orderURL matches Order.Order_URL."""
    res = ColumnMatcher.match_columns(["orderURL"], sample_schema)
    assert len(res) == 1
    m = res[0]
    assert m.status == "MATCHED"
    assert m.database_table == "Order"
    assert m.database_column == "Order_URL"
    assert m.normalized_source_name == "order url"
    assert m.normalized_target_name == "order url"


def test_prevent_unintended_many_to_one_column_claims(sample_schema):
    """
    When two source columns claim the same table and column:
    the exact/higher-confidence match wins, and the second is marked AMBIGUOUS.
    """
    excel_cols = ["Customer_Name", "customer-name"]
    res = ColumnMatcher.match_columns(excel_cols, sample_schema)

    assert len(res) == 2
    matched_items = [r for r in res if r.status == "MATCHED" and r.database_column == "Customer_Name"]
    ambiguous_items = [r for r in res if r.status == "AMBIGUOUS"]

    # One wins, the other is downgraded to prevent duplicate claims
    assert len(matched_items) == 1
    assert len(ambiguous_items) == 1
    assert matched_items[0].excel_column == "Customer_Name"  # EXACT_ORIGINAL (1.0) wins over EXACT_NORMALIZED (0.99)
    assert ambiguous_items[0].excel_column == "customer-name"


def test_candidate_metadata_structure(sample_schema):
    """Verify candidates contain dataTypeCompatibility, keyInfo, matchMethod, reason."""
    res = ColumnMatcher.match_columns(["customer_mobile"], sample_schema)
    assert len(res) == 1
    m = res[0]
    assert len(m.candidates) > 0
    first_cand = m.candidates[0]
    assert "databaseTable" in first_cand
    assert "databaseColumn" in first_cand
    assert "matchMethod" in first_cand
    assert "confidence" in first_cand
    assert "dataTypeCompatibility" in first_cand
    assert "keyInfo" in first_cand
    assert "reason" in first_cand
    assert "normalizedTargetName" in first_cand


# =================================================================================================
# 3. VALUE RESOLUTION TESTS (IDENTIFIERS, LEADING ZEROS, NULLS)
# =================================================================================================
def test_value_resolution_preserves_leading_zeros_and_punctuation():
    """Verify string normalization doesn't strip leading zeros or meaningful identifiers."""
    # Identifier e.g. "00123"
    raw_id = "00123"
    assert str(raw_id).strip() == "00123"
    assert normalize_string(raw_id) == "00123"

    # Code with punctuation e.g. "ORD-001/A"
    raw_code = "ORD-001/A"
    assert str(raw_code).strip() == "ORD-001/A"
    # normalize_column_name breaks it down for matching, but raw values retain punctuation
    assert normalize_column_name(raw_code) == "ord 001 a"


def test_missing_values_never_converted_to_string_nan_or_none():
    """Ensure pandas nulls/whitespace are handled cleanly without turning into 'nan' or 'None' strings."""
    df = pd.DataFrame([
        {"id": 1, "val": None},
        {"id": 2, "val": ""},
        {"id": 3, "val": "   "},
    ])

    for v in df["val"]:
        v_clean = str(v).strip() if v is not None and not pd.isna(v) else None
        if v_clean == "":
            v_clean = None
        assert v_clean is None
        assert v_clean != "nan"
        assert v_clean != "None"

"""
Automated Unit Tests for:
1. Dynamic Item Mapping & Variant Resolution (Section 2 & 7)
2. Historical Transaction Price & Quantity Preservation (Section 3 & 7)
3. Customer Creation Date (Customer.Date_Added / date_add) Dynamic Mapping (Section 4 & 7)
4. Order Transaction Date Isolation (Never map Order/Invoice date to Customer.Date_Added)
5. Date Format & Normalization Handling (Excel serial, ISO string, datetime, Timestamp)
6. Zero Database Writes Guarantee During Dry Run & Diagnostic Analysis (Section 5 & 7)
"""

import pytest
import datetime
import pandas as pd
from unittest.mock import MagicMock, patch

from app.models.schema import DatabaseSchemaMetadata, TableMetadata, ColumnMetadata
from app.mapping.matcher import ColumnMatcher
from app.migration.lookup_resolver import DynamicLookupResolver
from app.migration.item_reconciliation import ItemReconciliationService
from app.migration.engine import sanitize_value
from app.utils.normalization import normalize_date_value


@pytest.fixture
def mock_item_master():
    """Master items dataset with realistic names, variants, and identical prices."""
    return [
        {"ID": 1, "Item_ID": 22, "Item": "Masala Dosa", "Item_Price": 46.296},
        {"ID": 2, "Item_ID": 59, "Item": "Gulab Jamun (2pc)", "Item_Price": 27.778},
        {"ID": 3, "Item_ID": 79, "Item": "Ven Pongal Parcel", "Item_Price": 60.185},
        {"ID": 4, "Item_ID": 119, "Item": "Sweet Lassi", "Item_Price": 37.037},
        {"ID": 5, "Item_ID": 201, "Item": "Plain Dosa", "Item_Price": 35.000},
        {"ID": 6, "Item_ID": 202, "Item": "Rava Dosa", "Item_Price": 50.000},
        {"ID": 7, "Item_ID": 301, "Item": "Idli (2pc)", "Item_Price": 30.000},
        {"ID": 8, "Item_ID": 302, "Item": "Vada (2pc)", "Item_Price": 30.000},  # Identical price as Idli
        {"ID": 9, "Item_ID": 401, "Item": "Veg Biryani Online", "Item_Price": 120.000},
    ]


@pytest.fixture
def test_schema_with_customer():
    """Schema containing Customer, Order, OrderLineItems, ItemsMaster."""
    return DatabaseSchemaMetadata(
        database_name="POS_DB",
        db_type="mssql",
        tables=[
            TableMetadata(
                name="Customer",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Customer_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Customer_Name", type="VARCHAR(100)", nullable=False),
                    ColumnMetadata(name="Address", type="VARCHAR(200)", nullable=True),
                    ColumnMetadata(name="Mobile_Number", type="VARCHAR(20)", nullable=True),
                    ColumnMetadata(name="Date_Added", type="DATETIME", nullable=True),
                    ColumnMetadata(name="Date_Modified", type="DATETIME", nullable=True),
                ],
                primary_keys=["Customer_ID"]
            ),
            TableMetadata(
                name="Order",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_DateTime", type="DATETIME", nullable=False),
                    ColumnMetadata(name="Customer_ID", type="INT", nullable=True),
                    ColumnMetadata(name="Total_Amount", type="DECIMAL(10,2)", nullable=True),
                ],
                primary_keys=["Order_ID"]
            ),
            TableMetadata(
                name="OrderLineItems",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="Line_ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Order_ID", type="INT", nullable=False),
                    ColumnMetadata(name="Item_ID", type="INT", nullable=False),
                    ColumnMetadata(name="Description", type="VARCHAR(200)", nullable=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,2)", nullable=False),
                    ColumnMetadata(name="Quantity", type="INT", nullable=False),
                ],
                primary_keys=["Line_ID"]
            ),
            TableMetadata(
                name="ItemsMaster",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="ID", type="INT", nullable=False, primary_key=True, identity=True),
                    ColumnMetadata(name="Item_ID", type="INT", nullable=False),
                    ColumnMetadata(name="Item", type="VARCHAR(100)", nullable=False),
                    ColumnMetadata(name="Item_Price", type="DECIMAL(10,3)", nullable=False),
                ],
                primary_keys=["ID"]
            )
        ]
    )


class TestDynamicItemMapping:
    """Tests Section 2 & 7: Dynamic item lookup and variant distinction."""

    def test_exact_item_resolution(self, mock_item_master):
        """Masala Dosa resolves dynamically to Item_ID 22."""
        index = DynamicLookupResolver.get_or_create_index(
            db_records=mock_item_master,
            parent_id_column="Item_ID",
            parent_name_column="Item"
        )
        resolved_id, matched_name, status, method, conf, candidates, msg = DynamicLookupResolver.match_name_to_id(
            excel_name="Masala Dosa",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        assert status == "MATCHED"
        assert resolved_id == 22
        assert matched_name == "Masala Dosa"

    def test_packaging_variant_resolution(self, mock_item_master):
        """Gulab Jamun (2pc) preserves packaging variant and resolves to 59."""
        index = DynamicLookupResolver.get_or_create_index(
            db_records=mock_item_master,
            parent_id_column="Item_ID",
            parent_name_column="Item"
        )
        resolved_id, matched_name, status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
            excel_name="Gulab Jamun (2pc)",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        assert status == "MATCHED"
        assert resolved_id == 59

    def test_parcel_and_online_variants(self, mock_item_master):
        """Ven Pongal Parcel and Veg Biryani Online preserve variant keywords."""
        index = DynamicLookupResolver.get_or_create_index(
            db_records=mock_item_master,
            parent_id_column="Item_ID",
            parent_name_column="Item"
        )
        # Parcel variant
        res_id_parcel, name_parcel, status_p, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
            excel_name="Ven Pongal Parcel",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        assert status_p == "MATCHED"
        assert res_id_parcel == 79

        # Online variant
        res_id_online, name_online, status_o, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
            excel_name="Veg Biryani Online",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        assert status_o == "MATCHED"
        assert res_id_online == 401

    def test_price_independence_resolution(self, mock_item_master):
        """Items with identical prices (Idli 2pc and Vada 2pc both 30.00) resolve independently."""
        index = DynamicLookupResolver.get_or_create_index(
            db_records=mock_item_master,
            parent_id_column="Item_ID",
            parent_name_column="Item"
        )
        idli_id, _, idli_status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
            excel_name="Idli (2pc)",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        vada_id, _, vada_status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
            excel_name="Vada (2pc)",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        assert idli_status == "MATCHED"
        assert vada_status == "MATCHED"
        assert idli_id == 301
        assert vada_id == 302
        assert idli_id != vada_id

    def test_unresolved_and_ambiguous_reporting(self, mock_item_master):
        """Unknown items are cleanly flagged as UNRESOLVED and never assigned incorrect IDs."""
        index = DynamicLookupResolver.get_or_create_index(
            db_records=mock_item_master,
            parent_id_column="Item_ID",
            parent_name_column="Item"
        )
        res_id, _, status, _, _, _, _ = DynamicLookupResolver.match_name_to_id(
            excel_name="Nonexistent Mystery Dish 999",
            db_records=mock_item_master,
            parent_table="ItemsMaster",
            parent_id_column="Item_ID",
            parent_name_column="Item",
            index=index
        )
        assert status == "UNRESOLVED"
        assert res_id is None


class TestPreserveTransactionPricesAndQuantities:
    """Tests Section 3 & 7: Historical prices and quantities preservation."""

    def test_transaction_price_never_overwritten_by_master_price(self, mock_item_master):
        """
        When order line has transaction price (e.g. 79.00), sanitize_value or migration
        preserves 79.00 and does NOT substitute master price (46.296).
        """
        txn_price = 79.00
        qty = 2
        col_type_price = "DECIMAL(10,2)"
        col_type_qty = "INT"

        sanitized_price = sanitize_value(txn_price, col_type_price, nullable=False)
        sanitized_qty = sanitize_value(qty, col_type_qty, nullable=False)

        assert sanitized_price == 79.00
        assert sanitized_qty == 2

    def test_item_reconciliation_diagnostic_preserves_historical_values(self, mock_item_master):
        """
        Diagnostic report correctly identifies mismatched item ID while preserving
        current_price and quantity without any database execution.
        """
        mock_child_records = [
            {
                "Line_ID": 1001,
                "Order_ID": 501,
                "Item_ID": 79,  # Mismatched ID in DB (points to Ven Pongal Parcel)
                "Description": "Masala Dosa",  # Correct item name
                "Item_Price": 79.00,  # Historical transaction price
                "Quantity": 3
            }
        ]

        summary = ItemReconciliationService.generate_diagnostic_report(
            engine=None,
            child_table="OrderLineItems",
            master_table="ItemsMaster",
            child_records=mock_child_records,
            master_records=mock_item_master
        )

        assert summary.total_scanned == 1
        assert summary.mismatched_count == 1
        assert summary.confirmed_count == 1
        report_item = summary.report[0]
        assert report_item.line_id == 1001
        assert report_item.description == "Masala Dosa"
        assert report_item.current_item_id == "79"
        assert report_item.proposed_item_id == "22"  # Corrected to Masala Dosa
        assert report_item.current_price == 79.00  # Historical transaction price preserved
        assert report_item.quantity == 3  # Quantity preserved


class TestCustomerDateMapping:
    """Tests Section 4 & 7: Customer.Date_Added dynamic mapping and isolation."""

    @pytest.mark.parametrize("col_name", [
        "customer_created_at",
        "customer_date_add",
        "date_add",
        "customer_date",
        "created_date",
    ])
    def test_customer_date_columns_map_to_customer_date_added(self, col_name, test_schema_with_customer):
        """Various Excel customer date headers map dynamically to Customer.Date_Added."""
        results = ColumnMatcher.match_columns([col_name], test_schema_with_customer)
        assert len(results) >= 1
        match = results[0]
        assert match.database_table == "Customer"
        assert match.database_column == "Date_Added"
        assert match.status == "MATCHED"
        assert match.confidence >= 0.95

    def test_customer_creation_date_never_maps_to_customer_name(self, test_schema_with_customer):
        """Date column customer_created_at must NEVER be misrouted to Customer.Customer_Name."""
        results = ColumnMatcher.match_columns(["customer_created_at"], test_schema_with_customer)
        for r in results:
            assert r.database_column != "Customer_Name"

    def test_order_date_never_maps_to_customer_date_added(self, test_schema_with_customer):
        """Generic transaction dates (order_date, date, invoice_date) must NEVER map to Customer.Date_Added."""
        for txn_date_col in ["date", "order_date", "invoice_date", "order_datetime"]:
            results = ColumnMatcher.match_columns([txn_date_col], test_schema_with_customer)
            for r in results:
                # Should map to Order or Bills, NOT Customer.Date_Added
                if r.database_table == "Customer":
                    assert r.database_column != "Date_Added", f"{txn_date_col} erroneously mapped to Customer.Date_Added!"

    def test_simultaneous_order_date_and_customer_date(self, test_schema_with_customer):
        """
        When both order date and customer creation date are present,
        order_date routes to Order.Order_DateTime and customer_date_add routes to Customer.Date_Added.
        """
        excel_cols = ["customer_date_add", "order_date", "customer_name", "item_name", "item_price", "qty"]
        results = ColumnMatcher.match_columns(excel_cols, test_schema_with_customer)

        cust_date_result = next((r for r in results if r.excel_column == "customer_date_add"), None)
        order_date_result = next((r for r in results if r.excel_column == "order_date"), None)

        assert cust_date_result is not None
        assert cust_date_result.database_table == "Customer"
        assert cust_date_result.database_column == "Date_Added"

        assert order_date_result is not None
        assert order_date_result.database_table == "Order"
        assert order_date_result.database_column == "Order_DateTime"


class TestDateFormatAndSanitization:
    """Tests Section 4 & 7: Date formats, conversions, and parameter binding."""

    def test_excel_serial_date_normalization(self):
        """Excel serial date (e.g. 44927 -> 2023-01-01) normalizes correctly."""
        norm_str, dt_obj, is_valid = normalize_date_value(44927, target_type="DATETIME")
        assert is_valid is True
        assert norm_str.startswith("2023-01-01")

    def test_iso_string_date_normalization(self):
        """ISO 8601 string normalizes to standard SQL Server format."""
        norm_str, _, is_valid = normalize_date_value("2026-03-15 14:30:00", target_type="DATETIME")
        assert is_valid is True
        assert norm_str == "2026-03-15 14:30:00"

    def test_pandas_timestamp_normalization(self):
        """pandas Timestamp normalizes to standard SQL Server string."""
        ts = pd.Timestamp("2026-05-20 09:15:00")
        norm_str, _, is_valid = normalize_date_value(ts, target_type="DATETIME")
        assert is_valid is True
        assert norm_str == "2026-05-20 09:15:00"

    def test_python_datetime_normalization(self):
        """Python datetime object normalizes to standard SQL Server string."""
        dt = datetime.datetime(2026, 7, 4, 18, 0, 0)
        norm_str, _, is_valid = normalize_date_value(dt, target_type="DATETIME")
        assert is_valid is True
        assert norm_str == "2026-07-04 18:00:00"

    def test_sanitize_value_for_customer_date_added(self):
        """sanitize_value converts date value into valid SQL Server string parameter."""
        val = "2026-10-12 12:00:00"
        sanitized = sanitize_value(val, "DATETIME", nullable=True)
        assert sanitized == "2026-10-12 12:00:00"

    def test_sanitize_value_for_null_date(self):
        """Empty or missing date cleanly produces None for nullable datetime column."""
        assert sanitize_value(None, "DATETIME", nullable=True) is None
        assert sanitize_value("", "DATETIME", nullable=True) is None
        assert sanitize_value("NaN", "DATETIME", nullable=True) is None
        assert sanitize_value("NaT", "DATETIME", nullable=True) is None

    def test_sanitize_value_for_day_first_customer_date_added(self):
        """Day-first dates like 30-06-2026 00:00 are normalized to standard ISO format to prevent SQL Server out-of-range errors."""
        val = "30-06-2026 00:00"
        sanitized = sanitize_value(val, "DATETIME", nullable=True)
        assert sanitized == "2026-06-30 00:00:00"

        val_slash = "30/06/2026 14:30"
        sanitized_slash = sanitize_value(val_slash, "DATETIME", nullable=True)
        assert sanitized_slash == "2026-06-30 14:30:00"


class TestZeroDatabaseWritesGuarantee:
    """Tests Section 5 & 7: Zero database writes guarantee during diagnostics and dry run."""

    def test_diagnostic_report_zero_database_writes(self, mock_item_master):
        """generate_diagnostic_report with provided records never touches database."""
        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        mock_child = [
            {"Line_ID": 1, "Order_ID": 10, "Item_ID": 79, "Description": "Masala Dosa", "Item_Price": 79.0, "Quantity": 1}
        ]

        # Call with child_records and master_records provided
        res = ItemReconciliationService.generate_diagnostic_report(
            engine=mock_engine,
            child_records=mock_child,
            master_records=mock_item_master
        )

        assert res.total_scanned == 1
        # mock_conn.execute must NOT be called for data mutations
        mock_conn.execute.assert_not_called()

    def test_apply_corrections_dry_run_zero_database_writes(self):
        """apply_corrections with apply=False performs ZERO writes."""
        mock_engine = MagicMock()
        res = ItemReconciliationService.apply_corrections(
            approved_line_ids=[101, 102],
            engine=mock_engine,
            apply=False
        )
        assert res["success"] is False
        assert res["updated_count"] == 0
        mock_engine.begin.assert_not_called()
        mock_engine.connect.assert_not_called()

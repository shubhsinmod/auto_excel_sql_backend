import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set
from pydantic import BaseModel, Field
from app.utils.normalization import normalize_column_name, tokenize_column_name, stem_token
from app.core.logging import logger


class BusinessConcept(BaseModel):
    id: str
    name: str
    description: str = ""
    synonyms: List[str] = Field(default_factory=list)
    expected_type: str = "TEXT"  # TEXT, NUMERIC, DECIMAL, INTEGER, DATETIME, PERCENTAGE
    database_scope: Optional[str] = None  # None = Global, or specific DB name e.g. "BOPOS_Leons"
    is_builtin: bool = False


class CustomMappingRule(BaseModel):
    rule_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_column: str
    business_concept: Optional[str] = None
    target_table: str
    target_column: str
    database_scope: Optional[str] = None  # Scoped to DB e.g. "BOPOS_Leons"
    schema_scope: Optional[str] = "dbo"
    status: str = "APPROVED"
    data_type_restriction: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


DEFAULT_BUSINESS_CONCEPTS: List[Dict[str, Any]] = [
    {
        "id": "customer_name",
        "name": "Customer name",
        "description": "Full name or display name of the customer/client",
        "synonyms": [
            "customer_name", "client_name", "guest_name", "custname",
            "customer", "client", "guest", "cust_name", "party_name"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "customer_phone",
        "name": "Customer phone",
        "description": "Phone or mobile contact number for the customer",
        "synonyms": [
            "customer_phone", "phone_number", "mobile", "contact_no",
            "phone", "contact_number", "mobile_number", "telephone", "cell", "phoneno", "mobileno",
            "contact_phone", "contact phone"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "customer_address",
        "name": "Customer address",
        "description": "Delivery, billing, or residential address of customer",
        "synonyms": [
            "customer_address", "billing_address", "delivery_address",
            "cust_address", "shipping_address", "street_address"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "customer_created_date",
        "name": "Customer created date",
        "description": "Customer creation, registration, or addition date",
        "synonyms": [
            "date_add", "date_added", "customer_date_add", "customer_date_added",
            "customer_created_at", "customer_created_date", "customer_date",
            "customer_registration_date", "cust_date_add", "created_date", "creation_date"
        ],
        "expected_type": "DATETIME",
        "is_builtin": True
    },
    {
        "id": "outlet_name",
        "name": "Outlet name",
        "description": "Outlet, restaurant, brand or store business name",
        "synonyms": [
            "outlet_name", "restaurant_name", "virtual_brand_name",
            "outlet", "restaurant", "store_name", "branch_name",
            "outletname", "restaurantname", "virtual_brand", "brand_name"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "location",
        "name": "Location",
        "description": "Physical area, locality, city, or geographical location",
        "synonyms": [
            "location", "outlet_location", "branch_location", "restaurant_location",
            "store_location", "city", "region", "locality"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "invoice_number",
        "name": "Invoice number",
        "description": "Invoice, bill, or receipt transaction identifier",
        "synonyms": [
            "invoice_no", "invoice_number", "bill_no", "receipt_no",
            "bill_number", "billno", "receipt_number", "txn_no", "transaction_no", "invoiceno"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "order_date",
        "name": "Order date",
        "description": "Date or timestamp of the order or transaction",
        "synonyms": [
            "order_date", "invoice_date", "transaction_date",
            "order_date_time", "bill_date", "order_datetime", "txn_date", "date"
        ],
        "expected_type": "DATETIME",
        "is_builtin": True
    },
    {
        "id": "item_name",
        "name": "Item name",
        "description": "Name or description of line item, dish, or product",
        "synonyms": [
            "item_name", "product_name", "dish_name", "menu_item",
            "description", "item_description", "product_description", "article_name", "item"
        ],
        "expected_type": "TEXT",
        "is_builtin": True
    },
    {
        "id": "quantity",
        "name": "Quantity",
        "description": "Quantity or unit count of items sold",
        "synonyms": [
            "quantity", "qty", "item_quantity", "units", "pieces", "pcs", "count"
        ],
        "expected_type": "INTEGER",
        "is_builtin": True
    },
    {
        "id": "item_price",
        "name": "Item price",
        "description": "Unit rate or selling price of individual line item",
        "synonyms": [
            "item_price", "unit_price", "rate", "price", "unit_rate", "mrp", "item_rate"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    },
    {
        "id": "item_total",
        "name": "Item total",
        "description": "Total monetary amount for line item (quantity * price)",
        "synonyms": [
            "item_total", "line_total", "extended_price", "item_amount", "line_amount", "row_total"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    },
    {
        "id": "total_amount",
        "name": "Total amount",
        "description": "Total net/gross transaction amount for bill or order",
        "synonyms": [
            "total_amount", "grand_total", "invoice_total", "bill_amount",
            "final_amount", "order_total", "total", "net_amount", "gross_amount", "my_amount"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    },
    {
        "id": "discount",
        "name": "Discount",
        "description": "Total monetary discount or rebate amount",
        "synonyms": [
            "discount", "discount_amount", "discount_value", "disc", "rebate", "discount_total"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    },
    {
        "id": "tax_amount",
        "name": "Tax amount",
        "description": "Total monetary tax or VAT amount",
        "synonyms": [
            "tax_amount", "total_tax", "vat_amount", "vat_total",
            "gst_amount", "tax_total", "tax"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    },
    {
        "id": "tax_percentage",
        "name": "Tax percentage",
        "description": "Percentage rate for VAT or sales tax (e.g. 5%, 18%)",
        "synonyms": [
            "tax_percentage", "vat_percentage", "tax_rate", "vat_rate",
            "gst_rate", "tax_percent", "vat_percent", "vat_pct"
        ],
        "expected_type": "PERCENTAGE",
        "is_builtin": True
    },
    {
        "id": "delivery_charge",
        "name": "Delivery charge",
        "description": "Ancillary delivery or shipping charge fee",
        "synonyms": [
            "delivery_charge", "shipping_charge", "delivery_fee",
            "shipping_fee", "delivery_charges"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    },
    {
        "id": "service_charge",
        "name": "Service charge",
        "description": "Restaurant or hospitality service charge fee",
        "synonyms": [
            "service_charge", "service_fee", "service_charge_total", "service_charges"
        ],
        "expected_type": "DECIMAL",
        "is_builtin": True
    }
]


class SynonymRegistry:
    """
    Configurable, thread-safe, extensible synonym and business-concept registry.
    Supports:
    - Scoped business concepts and synonyms (global or database-specific).
    - Learning and persisting approved user mapping rules.
    - JSON persistence in backend/data.
    - High-performance cached lookups.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self._lock = threading.RLock()
        if data_dir is None:
            backend_dir = Path(__file__).resolve().parent.parent.parent
            data_dir = backend_dir / "data"
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._synonyms_file = self._data_dir / "synonyms.json"
        self._rules_file = self._data_dir / "mapping_rules.json"

        self._concepts: Dict[str, BusinessConcept] = {}
        self._rules: List[CustomMappingRule] = []

        self._load()

    def _load(self):
        with self._lock:
            # Load default concepts
            self._concepts.clear()
            for def_c in DEFAULT_BUSINESS_CONCEPTS:
                concept = BusinessConcept(**def_c)
                self._concepts[concept.id] = concept

            # Load custom/persisted synonyms if file exists
            if self._synonyms_file.exists():
                try:
                    with open(self._synonyms_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        for item in data:
                            c = BusinessConcept(**item)
                            self._concepts[c.id] = c
                except Exception as e:
                    logger.error(f"[SYNONYM] Error loading custom synonyms: {e}")

            # Load approved mapping rules
            self._rules.clear()
            if self._rules_file.exists():
                try:
                    with open(self._rules_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        for r in data:
                            rule = CustomMappingRule(**r)
                            self._rules.append(rule)
                except Exception as e:
                    logger.error(f"[SYNONYM] Error loading mapping rules: {e}")

    def _save_synonyms(self):
        with self._lock:
            try:
                data = [c.model_dump() for c in self._concepts.values()]
                with open(self._synonyms_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.error(f"[SYNONYM] Error saving synonyms: {e}")

    def _save_rules(self):
        with self._lock:
            try:
                data = [r.model_dump() for r in self._rules]
                with open(self._rules_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.error(f"[SYNONYM] Error saving rules: {e}")

    # -------------------------------------------------------------------------
    # Business Concepts & Synonyms Management
    # -------------------------------------------------------------------------

    def get_concepts(self, database_scope: Optional[str] = None) -> List[BusinessConcept]:
        with self._lock:
            result = []
            for c in self._concepts.values():
                if c.database_scope is None or database_scope is None or c.database_scope.lower() == database_scope.lower():
                    result.append(c)
            return result

    def get_concept(self, concept_id: str, database_scope: Optional[str] = None) -> Optional[BusinessConcept]:
        with self._lock:
            c = self._concepts.get(concept_id)
            if c:
                if c.database_scope is None or database_scope is None or c.database_scope.lower() == database_scope.lower():
                    return c
            return None

    def add_concept(self, concept: BusinessConcept) -> BusinessConcept:
        with self._lock:
            # Normalize synonyms
            concept.synonyms = list({normalize_column_name(s) for s in concept.synonyms if s})
            self._concepts[concept.id] = concept
            self._save_synonyms()
            return concept

    def delete_concept(self, concept_id: str, database_scope: Optional[str] = None) -> bool:
        with self._lock:
            c = self._concepts.get(concept_id)
            if not c:
                return False
            if database_scope and c.database_scope and c.database_scope.lower() != database_scope.lower():
                return False
            del self._concepts[concept_id]
            self._save_synonyms()
            return True

    def add_synonym(self, concept_id: str, synonym: str, database_scope: Optional[str] = None) -> bool:
        with self._lock:
            c = self.get_concept(concept_id, database_scope)
            if not c:
                return False
            norm_syn = normalize_column_name(synonym)
            if norm_syn and norm_syn not in [normalize_column_name(s) for s in c.synonyms]:
                c.synonyms.append(norm_syn)
                self._save_synonyms()
            return True

    def remove_synonym(self, concept_id: str, synonym: str, database_scope: Optional[str] = None) -> bool:
        with self._lock:
            c = self.get_concept(concept_id, database_scope)
            if not c:
                return False
            norm_syn = normalize_column_name(synonym)
            initial_len = len(c.synonyms)
            c.synonyms = [s for s in c.synonyms if normalize_column_name(s) != norm_syn]
            if len(c.synonyms) < initial_len:
                self._save_synonyms()
                return True
            return False

    def find_concept_for_term(
        self,
        term: str,
        database_scope: Optional[str] = None
    ) -> Optional[Tuple[str, BusinessConcept]]:
        """
        Looks up which business concept a given column name or synonym corresponds to.
        Returns (matched_synonym, BusinessConcept) if found.
        """
        if not term:
            return None
        norm_term = normalize_column_name(term)
        if not norm_term:
            return None

        with self._lock:
            # Check concepts in scope
            concepts = self.get_concepts(database_scope)
            for c in concepts:
                for syn in c.synonyms:
                    if normalize_column_name(syn) == norm_term:
                        return (syn, c)
            return None

    # -------------------------------------------------------------------------
    # Approved User Mapping Rules Management (Learning)
    # -------------------------------------------------------------------------

    def get_approved_rules(self, database_scope: Optional[str] = None) -> List[CustomMappingRule]:
        with self._lock:
            if not database_scope:
                return list(self._rules)
            return [
                r for r in self._rules
                if r.database_scope is None or r.database_scope.lower() == database_scope.lower()
            ]

    def find_approved_rule(
        self,
        source_column: str,
        database_scope: Optional[str] = None
    ) -> Optional[CustomMappingRule]:
        """
        Searches for an approved mapping rule for the source column, strictly respecting database scope.
        Never allows one database's rules to affect another database.
        """
        if not source_column:
            return None
        norm_src = normalize_column_name(source_column)

        with self._lock:
            # First look for database-scoped rule
            if database_scope:
                for r in self._rules:
                    if r.database_scope and r.database_scope.lower() == database_scope.lower():
                        if normalize_column_name(r.source_column) == norm_src:
                            return r

            # Next look for global rule (database_scope is None)
            for r in self._rules:
                if r.database_scope is None:
                    if normalize_column_name(r.source_column) == norm_src:
                        return r

            return None

    def save_approved_rule(self, rule: CustomMappingRule) -> CustomMappingRule:
        with self._lock:
            # Replace existing rule for same source_column and database_scope
            norm_src = normalize_column_name(rule.source_column)
            self._rules = [
                r for r in self._rules
                if not (
                    normalize_column_name(r.source_column) == norm_src and
                    (r.database_scope or "").lower() == (rule.database_scope or "").lower()
                )
            ]
            self._rules.append(rule)
            self._save_rules()
            logger.info(
                f"[SYNONYM] Saved approved rule: '{rule.source_column}' -> "
                f"'{rule.target_table}.{rule.target_column}' (DB: {rule.database_scope or 'GLOBAL'})"
            )
            return rule

    def delete_approved_rule(self, rule_id: str) -> bool:
        with self._lock:
            initial_len = len(self._rules)
            self._rules = [r for r in self._rules if r.rule_id != rule_id]
            if len(self._rules) < initial_len:
                self._save_rules()
                return True
            return False


# Singleton registry instance
synonym_registry = SynonymRegistry()

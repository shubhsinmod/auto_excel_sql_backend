import pytest
from app.migration.engine import invoice_identity_key

def test_invoice_identity_key():
    assert invoice_identity_key("INV-1001") == "inv-1001"
    assert invoice_identity_key("1057510") == "1057510"
    assert invoice_identity_key(1057510) == "1057510"
    assert invoice_identity_key("  INV-1001  ") == "inv-1001"

def test_composite_key_differentiation():
    """Verify that same invoice with different outlets yields different composite keys."""
    inv = "INV-1001"
    inv_k = invoice_identity_key(inv)
    
    outlet_a = 1
    outlet_b = 2
    
    key_a = (inv_k, outlet_a)
    key_b = (inv_k, outlet_b)
    
    assert key_a != key_b
    
    # In-memory store behaves as expected
    store = {}
    store[key_a] = {"BillNo": 101, "Order_ID": 501}
    
    assert key_a in store
    assert key_b not in store
    assert store.get(key_b) is None

def test_composite_key_override_same_outlet():
    """Verify that same invoice with same outlet matches for override."""
    inv = "INV-1001"
    inv_k = invoice_identity_key(inv)
    outlet_id = 1
    
    composite_key = (inv_k, outlet_id)
    store = {composite_key: {"BillNo": 101, "Order_ID": 501}}
    
    # Incoming row matches
    incoming_key = (invoice_identity_key("INV-1001"), 1)
    assert incoming_key in store
    assert store[incoming_key]["BillNo"] == 101

def test_composite_key_different_inv_same_outlet():
    """Verify that different invoice with same outlet does not collide."""
    inv_1 = "INV-1001"
    inv_2 = "INV-1002"
    outlet_id = 1
    
    key_1 = (invoice_identity_key(inv_1), outlet_id)
    key_2 = (invoice_identity_key(inv_2), outlet_id)
    
    store = {key_1: {"BillNo": 101}}
    assert key_2 not in store

"""Failure handling (design doc, "Failure handling"): payment fails -> keep the cart 10 min and offer
another saved method; tool error -> one retry, then tell the customer plainly and keep the session."""
from datetime import datetime, timedelta

import pytest

from foodagent import orders as orders_mod
from foodagent.agent import Agent
from foodagent.models import load_data
from foodagent.orders import MockPaymentGateway, OrderService, PaymentDeclined
from foodagent.parser import parse_rules
from foodagent.tools import Session, Tools, mentioned_method

NOW = datetime(2026, 9, 24, 18, 45)
EXAMPLE = ("Order dinner for six people. Two are vegetarian, one has a nut allergy. "
           "Keep the total under ₹2,000 and deliver it by 8 PM.")
CONSTRAINTS = {"headcount": 6, "groups": [{"count": 2, "diet": "veg"}], "budget_inr": {"max": 2000}, "deliver_by": "20:00"}
RESTAURANTS, PROFILE = load_data()
UPI, CARD = PROFILE["payment_methods"][:2]


def confirmed(svc: OrderService):
    s = Session(NOW, RESTAURANTS, PROFILE, svc)
    s.new_turn(EXAMPLE, parse_rules(EXAMPLE, NOW))
    t = Tools(s)
    bid = t.call("recommend_bundles", {"constraints": CONSTRAINTS})["bundles"][0]["bundle_id"]
    cart = t.call("confirm_cart", {"bundle_id": bid})
    return s, t, {"cart_id": cart["cart_id"], "confirm_token": cart["confirm_token"], "idempotency_key": "k1"}


def test_declined_payment_holds_the_cart_for_ten_minutes_then_another_method_works():
    svc = OrderService(payments=MockPaymentGateway({UPI}))
    s, _, args = confirmed(svc)
    cart = svc.carts[args["cart_id"]]
    with pytest.raises(PaymentDeclined) as e:
        svc.place_order(cart.cart_id, cart.token, "k1", NOW, UPI)
    assert e.value.held_until == NOW + timedelta(minutes=10) and cart.cart_id in svc.carts
    later = NOW + timedelta(minutes=8)                                  # past the 5-min token, inside the hold
    order = svc.place_order(cart.cart_id, cart.token, "k1", later, CARD)
    assert order["payment"] == CARD and len(svc.payments.charges) == 1


def test_place_order_tool_reports_the_decline_and_accepts_yes_with_the_other_method():
    s, t, args = confirmed(OrderService(payments=MockPaymentGateway({UPI})))
    s.new_turn("yes", {})
    out = t.call("place_order", {**args, "payment_method": UPI})
    assert out["payment_failed"] and out["other_payment_methods"] == [CARD] and out["cart_held_until"] == "18:55"
    assert s.state == "CONFIRMING"
    s.new_turn("yes, use the card instead", {})                         # "instead" is fine when switching method
    order = t.call("place_order", {**args, "payment_method": CARD})
    assert order["payment"] == CARD and s.state == "ORDERED"


def test_place_order_rejects_a_payment_method_the_customer_has_not_saved():
    s, t, args = confirmed(OrderService())
    s.new_turn("yes", {})
    assert "unknown payment_method" in t.call("place_order", {**args, "payment_method": "crypto wallet"})["error"]


def test_a_tool_failure_is_retried_once():
    s, t, _ = confirmed(OrderService())
    calls = []
    real = t.check_eta

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("logistics API timed out")
        return real(**kw)

    t.check_eta = flaky
    out = t.call("check_eta", {"restaurant_id": "r_spice_route"})
    assert "error" not in out and len(calls) == 2


def test_a_tool_that_keeps_failing_is_reported_plainly_and_the_session_survives():
    s, t, args = confirmed(OrderService())
    t.check_eta = lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))
    out = t.call("check_eta", {"restaurant_id": "r_spice_route"})
    assert out["temporary"] and "temporarily unavailable" in out["error"]
    assert s.state == "CONFIRMING" and s.cart is not None               # nothing the customer chose is lost


def test_a_failed_order_write_is_retried_without_double_charging(monkeypatch):
    writes = []

    def save_order(*a, **kw):
        writes.append(1)
        if len(writes) == 1:
            raise ConnectionError("database write failed")

    monkeypatch.setattr(orders_mod.db, "save_order", save_order)
    monkeypatch.setattr(orders_mod.db, "find_order", lambda *a, **kw: None)
    s, t, args = confirmed(OrderService(db_url="postgresql://unused"))
    s.new_turn("yes", {})
    order = t.call("place_order", args)
    assert order["order_id"].startswith("#") and len(writes) == 2
    assert len(s.orders.payments.charges) == 1


def test_mentioned_method():
    assert mentioned_method("use the card", [UPI, CARD]) == CARD
    assert mentioned_method("pay by upi", [UPI, CARD]) == UPI
    assert mentioned_method("yes", [UPI, CARD]) is None


def test_rule_agent_offers_the_next_saved_method_after_a_decline():
    agent = Agent(NOW, use_llm=False, orders=OrderService(payments=MockPaymentGateway({UPI})))
    agent.handle(EXAMPLE)
    agent.handle("A")
    assert f"Pay with {UPI}" in agent.handle("confirm")
    reply = agent.handle("yes")
    assert "didn't go through" in reply and f"Pay with {CARD} instead" in reply and agent.state == "CONFIRMING"
    assert "Order placed" in agent.handle("yes") and agent.state == "ORDERED"


def test_rule_agent_switches_method_on_request():
    agent = Agent(NOW, use_llm=False)
    agent.handle(EXAMPLE)
    agent.handle("A")
    agent.handle("confirm")
    assert f"Switched to {CARD}" in agent.handle("pay with card")
    assert "Order placed" in agent.handle("yes")
    assert agent.orders.orders_by_key[agent.idem_key]["payment"] == CARD

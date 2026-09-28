"""Postgres storage (db.py): normalized schema, seeding, derived allergens, orders, chat log.

Runs against its own database (TEST_DATABASE_URL, default postgresql://localhost/foodagent_test),
reloaded from the core seed file, so it never touches the app's data.
"""
import os
import secrets
from datetime import datetime

import pytest

from foodagent import db
from foodagent.engine import recommend_bundles
from foodagent.models import DATA_FILE, Constraints, catalog_from_seed, read_json
from foodagent.orders import OrderService
from foodagent.parser import parse_rules

URL = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/foodagent_test")
NOW = datetime(2026, 9, 24, 18, 45)


def reload() -> None:
    db.init(URL, reset=True, files=[DATA_FILE])


@pytest.fixture(scope="module", autouse=True)
def seeded():
    try:
        db.create_database(URL)
    except db.DatabaseError as exc:
        pytest.skip(str(exc))
    reload()


def item(code: str):
    return next(i for r in db.load_catalog(URL)[0] for i in r.items if i.id == code)


def comparable(i) -> dict:
    """Lists come back sorted and allergens merged across sources; compare what the engine uses."""
    d = dict(i.__dict__, allergens=sorted(i.allergens()))
    for k in ("contains", "may_contain"):
        d.pop(k)
    for k in ("ingredients", "primary", "famous_in", "tags"):
        d[k] = sorted(d[k])
    return d


def place(text: str, key: str) -> dict:
    restaurants, profile = db.load_catalog(URL)
    c = Constraints()
    c.update(parse_rules(text, NOW))
    bundle = recommend_bundles(c, restaurants, NOW, profile).bundles[0]
    svc = OrderService(db_url=URL)
    cart = svc.confirm_cart(bundle, c, NOW)
    return svc.place_order(cart.cart_id, cart.token, key, NOW)


def test_catalog_matches_the_seed_file():
    got, got_profile = db.load_catalog(URL)
    want, want_profile = catalog_from_seed(read_json())
    assert got_profile == want_profile
    assert [r.id for r in got] == [r.id for r in want]
    for g, w in zip(got, want):
        assert {k: v for k, v in g.__dict__.items() if k != "items"} == {k: v for k, v in w.__dict__.items() if k != "items"}
        assert [comparable(i) for i in g.items] == [comparable(i) for i in w.items]


def test_recipe_allergens_count_even_if_the_restaurant_did_not_declare_them():
    with db.connect(URL) as conn:
        dish_id, = conn.execute("SELECT id FROM dish WHERE code = 'sr_kp'").fetchone()        # Kadai Paneer
        conn.execute("DELETE FROM dish_allergen WHERE dish_id = %s AND source = 'declared'", [dish_id])
        conn.execute("INSERT INTO dish_ingredient (dish_id, ingredient_id) "
                     "SELECT %s, id FROM ingredient WHERE name = 'cashew'", [dish_id])
    assert {"dairy", "tree_nut"} <= item("sr_kp").allergens()                                # paneer + the added cashew
    reload()


def test_price_changes_are_logged():
    with db.connect(URL) as conn:
        conn.execute("UPDATE dish_variant SET price_paise = price_paise + 1000 "
                     "WHERE dish_id = (SELECT id FROM dish WHERE code = 'sr_kp')")
        prices = [p for p, in conn.execute(
            "SELECT l.price_paise FROM dish_variant_price_log l JOIN dish_variant v ON v.id = l.variant_id "
            "JOIN dish d ON d.id = v.dish_id WHERE d.code = 'sr_kp' ORDER BY l.id")]
    assert prices == [29000, 30000]
    reload()


def test_init_keeps_edits_unless_reset():
    with db.connect(URL) as conn:
        conn.execute("UPDATE dish_variant SET in_stock = false WHERE dish_id = (SELECT id FROM dish WHERE code = 'sr_kp')")
    db.init(URL, files=[DATA_FILE])
    assert item("sr_kp").in_stock is False
    reload()
    assert item("sr_kp").in_stock is True


def test_order_is_saved_in_paise_and_a_retry_returns_it_after_restart():
    key = f"test:{secrets.token_hex(6)}"
    order = place("dinner for 4, all veg, under 1500 by 8pm", key)
    fresh = OrderService(db_url=URL)                     # new process: in-memory idempotency map is empty
    assert fresh.place_order("gone", "gone", key, NOW) == order
    with db.connect(URL) as conn:
        total, lines = conn.execute("SELECT total_paise, (SELECT count(*) FROM order_item WHERE order_id = o.id) "
                                    "FROM orders o WHERE idempotency_key = %s", [key]).fetchone()
    assert total == round(order["total"] * 100) and lines == len(order["items"])


def test_orders_survive_a_catalog_reload():
    key = f"test:{secrets.token_hex(6)}"
    order = place("dinner for 2 under 1000 by 9pm", key)
    reload()
    assert db.find_order(key, URL) == order


def test_chat_turns_feed_the_session_view():
    sid = f"test-{secrets.token_hex(4)}"
    for n, (state, ms) in enumerate([("GATHERING", 5), ("RECOMMENDING", 900), ("CONFIRMING", 50), ("ORDERED", 40)]):
        db.save_request({"request_id": secrets.token_hex(4), "ts": NOW.replace(minute=n).isoformat() + "+05:30",
                         "session_id": sid, "agent": "Agent", "message": "m", "reply": "r", "state": state,
                         "tools": [{"tool": "x", "input": {"at": NOW}}], "error": None, "ms": ms}, URL)
    with db.connect(URL) as conn:
        row = conn.execute("SELECT turns, first_recommendation_turn, first_recommendation_ms, order_turn "
                           "FROM chat_session WHERE session_id = %s", [sid]).fetchone()
        tools, = conn.execute("SELECT tools FROM chat_requests WHERE session_id = %s LIMIT 1", [sid]).fetchone()
    assert row == (4, 2, 900, 4)
    assert tools[0]["tool"] == "x"


def test_metrics_report_scores_sessions_against_the_doc_targets():
    from foodagent.metrics import session_metrics
    with db.connect(URL) as conn:
        conn.execute("DELETE FROM chat_requests")
    sessions = {  # session -> states after each turn, first-recommendation latency
        "ordered-fast": (["RECOMMENDING", "EDITING", "CONFIRMING", "ORDERED"], 1200),
        "ordered-slow": (["GATHERING", "RECOMMENDING", "EDITING", "EDITING", "CONFIRMING", "ORDERED"], 5000),
        "browsed": (["RECOMMENDING", "RECOMMENDING"], 800),
        "no-headcount": (["GATHERING"], None),
    }
    for sid, (states, rec_ms) in sessions.items():
        for n, state in enumerate(states):
            ms = rec_ms if state == "RECOMMENDING" and states.index(state) == n else 30
            db.save_request({"request_id": f"{sid}-{n}", "ts": NOW.replace(minute=n).isoformat() + "+05:30",
                             "session_id": sid, "agent": "Agent", "message": "m", "reply": "r", "state": state,
                             "tools": [], "error": "Boom" if sid == "browsed" and n == 1 else None, "ms": ms}, URL)
    m = session_metrics(URL)
    assert (m["sessions"], m["sessions_with_recommendation"], m["sessions_with_order"]) == (4, 3, 2)
    assert m["chat_to_order_conversion"] == 0.667 and m["targets"]["chat_to_order_conversion"].startswith("met")
    assert m["median_turns_to_order"] == 5.0 and m["targets"]["median_turns_to_order"].startswith("MISSED")
    assert m["p95_first_recommendation_ms"] == 5000 and m["median_first_recommendation_ms"] == 1200
    assert m["turn_error_rate"] == round(1 / 13, 3)
    assert session_metrics(URL, agent="Orchestrator")["sessions"] == 0

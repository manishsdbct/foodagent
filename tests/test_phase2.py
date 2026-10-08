"""Phase 2 checks: ETA model, DP bundler, OrderConstraints schema, tools, orchestrator, sessions, web, eval gate."""
import json
import threading
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from foodagent import eta
from foodagent.bundler import EXTRA_SHARE, SLACK, dp_bundles, is_balance
from foodagent.engine import block_reason, recommend_bundles, score_item, violations
from foodagent.eval import ALLERGY_PARAPHRASES, run as run_eval
from foodagent.models import Constraints, load_data
from foodagent.orchestrator import Orchestrator, unverified_amounts
from foodagent.orders import OrderService
from foodagent.parser import parse_rules
from foodagent.schema import OrderConstraints
from foodagent.session import SessionStore, run_turn
from foodagent.tools import Session, Tools, tool_definitions
from foodagent.web import build_handler

NOW = datetime(2026, 9, 24, 18, 45)
EXAMPLE = ("Order dinner for six people. Two are vegetarian, one has a nut allergy. "
           "Keep the total under ₹2,000 and deliver it by 8 PM.")
RESTAURANTS, PROFILE = load_data()
BY_ID = {r.id: r for r in RESTAURANTS}


def constraints(text: str, now: datetime = NOW) -> Constraints:
    c = Constraints()
    c.update(parse_rules(text, now))
    return c


# ---------------------------------------------------------------- ETA
def test_eta_matches_the_worked_example():
    e = eta.check_eta(BY_ID["r_spice_route"], NOW, NOW.replace(hour=20, minute=0))
    assert f"{e.eta:%H:%M}" == "19:30" and e.deadline_ok and e.margin_min == 30
    assert e.eta_min < e.eta < e.eta_max


def test_peak_hour_is_slower_and_needs_a_bigger_buffer():
    r = BY_ID["r_spice_route"]
    off, peak = eta.check_eta(r, NOW), eta.check_eta(r, NOW.replace(hour=19, minute=30))
    assert (peak.eta - NOW.replace(hour=19, minute=30)) > (off.eta - NOW)
    assert peak.buffer_min == 20 and eta.buffer_min(NOW.replace(hour=17)) == 10


# ---------------------------------------------------------------- DP bundler
def test_dp_bundles_meet_every_stage4_rule():
    c = constraints(EXAMPLE)
    for r in RESTAURANTS:
        eligible = [i for i in r.items if block_reason(i, c) is None]
        scores = {i.id: score_item(i, r, c, PROFILE, 3000) for i in eligible}
        for b in dp_bundles(r, eligible, scores, c):
            assert violations(b, c) == [] or "over budget" not in " ".join(violations(b, c))
            assert 6 <= b.main_servings <= 6 + SLACK and 6 <= b.carb_servings <= 6 + SLACK
            extras = sum(l.item.price * l.qty for l in b.lines if l.item.course in ("starter", "dessert"))
            assert extras <= EXTRA_SHARE * b.subtotal
            if any(is_balance(i) for i in eligible):
                assert any(is_balance(l.item) for l in b.lines)


def test_example_bundles_give_vegetarians_choice_and_meat_eaters_a_dish():
    c = constraints(EXAMPLE)
    rec = recommend_bundles(c, RESTAURANTS, NOW, PROFILE)
    spice = next(b for b in rec.bundles if b.restaurant.id == "r_spice_route")
    assert spice.veg_main_dishes >= 2
    assert any(not l.item.is_veg for l in spice.lines)
    assert len({cu for b in rec.bundles for cu in b.restaurant.cuisines}) >= 2  # cuisine diversity
    assert [b.bundle_id for b in rec.bundles] == ["b_1", "b_2", "b_3"]
    assert all(any(code.startswith("under_budget_by_") for code in b.reasons) for b in rec.bundles)


def test_severe_nut_allergy_drops_shared_fryer_kitchen():
    mild = recommend_bundles(constraints("dinner for 4, nut allergy"), RESTAURANTS, NOW, PROFILE, k=6)
    severe = recommend_bundles(constraints("dinner for 4, severe nut allergy"), RESTAURANTS, NOW, PROFILE, k=6)
    assert "r_wok" in {b.restaurant.id for b in mild.bundles}
    assert "r_wok" not in {b.restaurant.id for b in severe.bundles}


def test_egg_in_a_veg_bakery_item_is_caught():
    cake = next(i for i in BY_ID["r_chaat"].items if i.id == "dc_cake")
    assert block_reason(cake, constraints("all veg for 4")) == "not vegetarian"
    assert block_reason(cake, constraints("all veg for 4, eggetarian")) is None


# ---------------------------------------------------------------- schema
def test_order_constraints_round_trip_matches_the_doc_shape():
    oc = OrderConstraints.from_constraints(constraints(EXAMPLE))
    d = oc.model_dump(exclude_none=True)
    assert d["headcount"] == 6 and d["budget_inr"]["max"] == 2000 and d["deliver_by"] == "20:00"
    assert {"count": 1, "diet": "any", "allergens": ["peanut", "tree_nut"]} in d["groups"]
    c = oc.to_constraints(NOW)
    assert (c.headcount, c.veg_count, c.allergens, c.budget_max) == (6, 2, {"peanut", "tree_nut"}, 2000)


def test_schema_rejects_malformed_constraints():
    with pytest.raises(ValidationError):
        OrderConstraints.model_validate({"headcount": 2, "groups": [{"count": 3, "diet": "veg"}]})
    with pytest.raises(ValidationError):
        OrderConstraints.model_validate({"headcount": 2, "groups": [{"count": 1, "allergens": ["nuts"]}]})
    with pytest.raises(ValidationError):
        OrderConstraints.model_validate({"headcount": 2, "deliver_by": "eight"})


# ---------------------------------------------------------------- tools
def session_for(text: str = EXAMPLE) -> tuple[Session, Tools]:
    s = Session(NOW, RESTAURANTS, PROFILE, OrderService())
    s.new_turn(text, parse_rules(text, NOW))
    return s, Tools(s)


LLM_CONSTRAINTS = {"headcount": 6, "groups": [{"count": 2, "diet": "veg"}],  # the LLM "forgot" the allergy
                   "budget_inr": {"max": 2000}, "deliver_by": "20:00"}


def test_allergy_typed_by_the_customer_survives_an_llm_that_drops_it():
    s, t = session_for()
    out = t.call("recommend_bundles", {"constraints": LLM_CONSTRAINTS})
    assert s.constraints.allergens == {"peanut", "tree_nut"}
    names = [i["name"] for b in out["bundles"] for i in b["items"]]
    assert "Butter Chicken" not in names and "Paneer Butter Masala" not in names
    assert "assumption" in out and "allergen_note" in out


def test_tool_flow_and_explicit_yes_gate():
    s, t = session_for()
    out = t.call("recommend_bundles", {"constraints": LLM_CONSTRAINTS})
    bid = next(b["bundle_id"] for b in out["bundles"] if b["restaurant_id"] == "r_spice_route")
    refused = t.call("modify_bundle", {"bundle_id": bid, "edits": [{"op": "add", "item": "butter chicken"}]})
    assert refused["applied"] is False and "can't add" in refused["message"]
    cart = t.call("confirm_cart", {"bundle_id": bid})
    assert cart["price"]["total"] <= 2000 and "ALLERGY" in cart["order_note"]
    args = {"cart_id": cart["cart_id"], "confirm_token": cart["confirm_token"], "idempotency_key": "k1"}
    assert "error" in t.call("place_order", args)                 # same turn as the summary
    s.new_turn("yes but add raita", {})
    assert "error" in t.call("place_order", args)                 # not a clean yes
    s.new_turn("yes", {})
    order = t.call("place_order", args)
    assert order["order_id"].startswith("#") and s.state == "ORDERED"
    assert t.call("place_order", args) == order                   # idempotent retry


def test_confirm_cart_offers_a_safe_substitute_when_out_of_stock():
    s, t = session_for()
    out = t.call("recommend_bundles", {"constraints": LLM_CONSTRAINTS})
    b = s.bundles[out["bundles"][0]["bundle_id"]]
    item = next(l.item for l in b.lines if l.item.course == "main")
    item.in_stock = False
    try:
        res = t.call("confirm_cart", {"bundle_id": b.bundle_id})
        assert "out of stock" in res["error"] and "substitute" in res["error"]
    finally:
        item.in_stock = True


def test_tool_definitions_are_well_formed():
    defs = tool_definitions()
    assert [d["name"] for d in defs] == ["get_user_context", "recommend_bundles", "modify_bundle",
                                         "check_eta", "confirm_cart", "place_order"]
    json.dumps(defs)
    assert "$defs" in defs[1]["input_schema"]


# ---------------------------------------------------------------- orchestrator (scripted fake Claude)
def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(i, name, args):
    return SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=args)


class FakeClient:
    """Plays back a script of responses; each step may be a callable that sees the messages."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # snapshot: the agent keeps appending
        step = self.script.pop(0)
        step = step(kw["messages"]) if callable(step) else step
        blocks = step if isinstance(step, list) else [step]
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(stop_reason=stop, content=blocks)


def last_tool_result(messages):
    return json.loads(messages[-1]["content"][-1]["content"])


def test_orchestrator_runs_the_whole_order_through_tools():
    def present(msgs):
        r = last_tool_result(msgs)
        b = r["bundles"][0]
        return text_block(f"A. {b['restaurant']} — ₹{b['price']['total_display']:,}, ETA {b['eta']}. Under your ₹2,000.")

    def summary(msgs):
        r = last_tool_result(msgs)
        return text_block(f"Total ₹{r['price']['total_display']:,}, arriving {r['eta']}. Place it?")

    state = {}

    def order(msgs):
        state["cart"] = next(json.loads(b["content"]) for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                             for b in m["content"] if "cart_id" in b["content"])
        return tool_block(4, "place_order", {"cart_id": state["cart"]["cart_id"],
                                             "confirm_token": state["cart"]["confirm_token"], "idempotency_key": "x"})

    client = FakeClient([
        [tool_block(1, "get_user_context", {}), tool_block(2, "recommend_bundles", {"constraints": LLM_CONSTRAINTS})],
        present,
        tool_block(3, "confirm_cart", {"bundle_id": "b_1"}),
        summary,
        order,
        lambda msgs: text_block(f"Order placed — {last_tool_result(msgs)['order_id']}."),
    ])
    agent = Orchestrator(NOW, client=client)
    assert "A." in agent.handle(EXAMPLE)
    assert "Place it?" in agent.handle("Option A please, confirm")
    assert agent.state == "CONFIRMING"
    assert "Order placed" in agent.handle("yes")
    assert agent.state == "ORDERED"
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["thinking"] == {"type": "adaptive"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    # tool results for parallel calls go back in one user message
    assert len(client.calls[1]["messages"][-1]["content"]) == 2


def test_orchestrator_replaces_a_reply_with_invented_prices():
    client = FakeClient([
        tool_block(1, "recommend_bundles", {"constraints": LLM_CONSTRAINTS}),
        text_block("A. Spice Route — ₹1,234"),         # not from any tool
        text_block("A. Spice Route — ₹999"),           # still wrong after the rewrite request
    ])
    agent = Orchestrator(NOW, client=client)
    reply = agent.handle(EXAMPLE)
    assert "₹1,234" not in reply and "₹999" not in reply and reply.startswith("A. ")
    assert "automated check" in client.calls[2]["messages"][-1]["content"]


def test_orchestrator_caps_tool_calls_per_turn():
    client = FakeClient([tool_block(i, "get_user_context", {}) for i in range(10)] + [text_block("Done.")])
    agent = Orchestrator(NOW, client=client)
    assert agent.handle("hi") == "Done."
    real = [row for row in agent.session.trace if row["tool"] == "get_user_context"]
    assert len(real) == 8


def test_unverified_amounts():
    assert unverified_amounts("₹1,887 total, under ₹2,000", {1887.0, 2000.0}) == []
    assert unverified_amounts("only Rs 150 more", {1887.0}) == ["Rs 150"]


@pytest.mark.parametrize("text,want", ALLERGY_PARAPHRASES)
def test_allergy_paraphrases(text, want):
    """Everyday phrasings ("allergic to fish", "can't have dairy", "no seafood") and mentions that are not
    allergies ("we love fish curry", "nobody is allergic to fish")."""
    assert parse_rules(text, NOW)["allergens"] == want


def test_orchestrator_sends_cards_before_the_reply_is_written():
    client = FakeClient([tool_block(1, "recommend_bundles", {"constraints": LLM_CONSTRAINTS}),
                         lambda msgs: text_block("I read the nut allergy as peanut and tree nut. Which one?")])
    agent = Orchestrator(NOW, client=client, cards=True)
    seen = []
    reply, row = run_turn(agent, "s", EXAMPLE, on_view=lambda v: seen.append((v["tool"], len(client.calls))))
    assert seen == [("recommend_bundles", 1)]          # drawn after the first model call, before the second
    assert row["first_view_ms"] is not None and row["first_view_ms"] <= row["ms"]
    first = client.calls[0]
    assert "[Customer profile" in first["messages"][0]["content"]  # no get_user_context round trip
    assert "Do not repeat those details" in first["system"][0]["text"]
    assert agent.on_view is None


def test_web_chat_stream_sends_view_then_done():
    def agent():
        return Orchestrator(NOW, client=FakeClient([tool_block(1, "recommend_bundles", {"constraints": LLM_CONSTRAINTS}),
                                                    text_block("Three nut-free options. Which one?")]), cards=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(SessionStore(agent)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/chat/stream",
                                     json.dumps({"message": EXAMPLE}).encode(), {"content-type": "application/json"})
        lines = [json.loads(ln) for ln in urllib.request.urlopen(req).read().splitlines()]
        assert [m["type"] for m in lines] == ["view", "done"]
        assert [b["t"] for b in lines[0]["view"]][:2] == ["look", "bundle"] and lines[0]["state"] == "RECOMMENDING"
        assert lines[1]["reply"].startswith("Three nut-free") and lines[1]["view"] == lines[0]["view"]
    finally:
        server.shutdown()


# ---------------------------------------------------------------- sessions & web
def test_session_store_expires_idle_chats():
    clock = [0.0]
    store = SessionStore(lambda: object(), ttl_s=10, clock=lambda: clock[0])
    sid, a = store.get(None)
    assert store.get(sid) == (sid, a)
    clock[0] = 11
    sid2, b = store.get(sid)
    assert sid2 != sid and b is not a


def test_web_chat_end_to_end():
    from foodagent.agent import Agent
    store = SessionStore(lambda: Agent(NOW, use_llm=False))
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(msg, sid=None):
        req = urllib.request.Request(base + "/api/chat", json.dumps({"session_id": sid, "message": msg}).encode(),
                                     {"content-type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    try:
        assert b"Food assistant" in urllib.request.urlopen(base + "/").read()
        r = post(EXAMPLE)
        assert r["state"] == "RECOMMENDING" and "A." in r["reply"]
        r = post("A", r["session_id"])
        assert r["state"] == "EDITING"
    finally:
        server.shutdown()


# ---------------------------------------------------------------- release gate
def test_offline_eval_release_gate():
    report = run_eval()
    assert report["requests"] >= 200
    assert report["allergen_violations"] == report["diet_violations"] == 0
    assert report["budget_breaches"] == report["deadline_breaches"] == 0
    assert report["parse_accuracy"] >= 0.95
    assert report["gate_pass"], report["_violations"][:5]

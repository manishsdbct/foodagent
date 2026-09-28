"""Phase 1 checks. Release gate from the design doc: zero allergen, diet, budget or deadline breaches."""
from datetime import datetime

import pytest

from foodagent.agent import Agent
from foodagent.engine import modify_bundle, recommend_bundles, search_dishes, violations
from foodagent.models import Constraints, load_data
from foodagent.orders import OrderError, OrderService
from foodagent.parser import parse, parse_rules

NOW = datetime(2026, 9, 24, 18, 45)
EXAMPLE = ("Order dinner for six people. Two are vegetarian one has nut allergy, "
           "keep the total under 2000 and deliver it by 8PM")
RESTAURANTS, PROFILE = load_data()


def example_constraints() -> Constraints:
    c = Constraints()
    c.update(parse_rules(EXAMPLE, NOW))
    return c


# ---------------------------------------------------------------- parser
def test_parser_reads_the_example():
    c = example_constraints()
    assert c.headcount == 6
    assert c.veg_count == 2
    assert c.allergens == {"peanut", "tree_nut"}
    assert c.budget_max == 2000
    assert c.deliver_by == NOW.replace(hour=20, minute=0)
    assert c.meal == "dinner"
    assert any("tree nuts" in n for n in c.notes)


@pytest.mark.parametrize("text,key,value", [
    ("suggest me spiccy paneer option", "spice_min", 3),
    ("something spicssy", "spice_min", 3),
    ("extra spicy please", "spice_min", 4),
    ("not too spicy, kids are eating", "spice_max", 1),
    ("budget 2.5k", "budget_max", 2500),
    ("under rs 1,800", "budget_max", 1800),
    ("by 8:30 pm", "deliver_by", NOW.replace(hour=20, minute=30)),
    ("by 8", "deliver_by", NOW.replace(hour=20, minute=0)),
    ("all veg for 4 people", "all_veg", True),
    ("famous in delhi", "region", "delhi"),
    ("no mushroom please", "exclude_ingredients", ["mushroom"]),
])
def test_parser_phrasings(text, key, value):
    out = parse_rules(text, NOW)
    if key:
        assert out[key] == value


def test_peanut_is_not_read_as_generic_nut():
    out = parse_rules("one person has a peanut allergy", NOW)
    assert out["allergens"] == {"peanut"}


def test_egg_allergy_is_not_an_ingredient_wish():
    out = parse_rules("2 people, egg allergy", NOW)
    assert "egg" in out["allergens"] and "egg" not in out["include_ingredients"]


def test_llm_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid")
    monkeypatch.setattr("foodagent.parser.parse_with_claude", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    fields, source = parse(EXAMPLE, NOW)
    assert source == "rules" and fields["headcount"] == 6


def test_llm_result_always_keeps_rule_allergens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("foodagent.parser.parse_with_claude", lambda *a: {"headcount": 6, "allergens": set()})
    fields, source = parse(EXAMPLE, NOW)
    assert source == "claude" and {"peanut", "tree_nut"} <= fields["allergens"]


# ---------------------------------------------------------------- engine
def test_example_bundles_satisfy_every_hard_constraint():
    c = example_constraints()
    rec = recommend_bundles(c, RESTAURANTS, NOW, PROFILE)
    assert len(rec.bundles) == 3
    assert len({b.restaurant.id for b in rec.bundles}) == 3  # diversity
    for b in rec.bundles:
        assert violations(b, c) == []
        assert b.total <= 2000
        assert b.eta <= c.deliver_by
        assert all(not (l.item.allergens() & c.allergens) for l in b.lines)
        assert b.veg_main_servings >= 2 and b.main_servings >= 6 and b.carb_servings >= 6


def test_late_restaurant_is_a_near_miss_not_an_option():
    rec = recommend_bundles(example_constraints(), RESTAURANTS, NOW, PROFILE)
    assert "Tandoor Nights" not in [b.restaurant.name for b in rec.bundles]
    assert any(name == "Tandoor Nights" and "misses" in why for name, why, _ in rec.near_misses)


def test_impossible_budget_returns_near_misses():
    c = example_constraints()
    c.budget_max = 600
    rec = recommend_bundles(c, RESTAURANTS, NOW, PROFILE)
    assert rec.bundles == [] and rec.near_misses


def test_unverified_allergen_data_is_excluded_for_allergic_diners():
    rec = recommend_bundles(example_constraints(), RESTAURANTS, NOW, PROFILE)
    names = [l.item.name for b in rec.bundles for l in b.lines]
    assert "Paneer Pakora" not in names


def test_spicy_paneer_search():
    c = Constraints()
    c.update(parse_rules("suggest me spiccy paneer option", NOW))
    hits = search_dishes(c, RESTAURANTS, NOW, PROFILE)
    assert hits
    for h in hits:
        assert "paneer" in h.item.primary and h.item.spice >= 3
    assert "Paneer Butter Masala" not in [h.item.name for h in hits]  # spice 1


def test_search_respects_allergy():
    c = Constraints()
    c.update(parse_rules("something sweet famous in delhi, nut allergy", NOW))
    hits = search_dishes(c, RESTAURANTS, NOW, PROFILE)
    assert all(not (h.item.allergens() & {"peanut", "tree_nut"}) for h in hits)
    assert "Rabri Jalebi" not in [h.item.name for h in hits]


# ---------------------------------------------------------------- edits & ordering
def spice_route_bundle(c):
    rec = recommend_bundles(c, RESTAURANTS, NOW, PROFILE)
    return next(b for b in rec.bundles if b.restaurant.id == "r_spice_route")


def test_swap_naan_for_rice_restores_carb_coverage():
    c = example_constraints()
    b = spice_route_bundle(c)
    new, msg = modify_bundle(b, "swap naan for more rice", c)
    assert "Swapped" in msg
    assert not any("Naan" in l.item.name for l in new.lines)
    assert new.carb_servings >= 6 and violations(new, c) == []


def test_adding_an_unsafe_dish_is_refused():
    c = example_constraints()
    b = spice_route_bundle(c)
    new, msg = modify_bundle(b, "add butter chicken", c)
    assert new is b and "can't add" in msg


def test_unknown_dish_is_not_fuzzy_matched_to_a_different_one():
    c = example_constraints()
    b = spice_route_bundle(c)
    new, msg = modify_bundle(b, "add chicken 65", c)
    assert new is b and "doesn't have" in msg


def test_order_needs_token_and_is_idempotent():
    c = example_constraints()
    b = spice_route_bundle(c)
    svc = OrderService()
    cart = svc.confirm_cart(b, c, NOW)
    with pytest.raises(OrderError):
        svc.place_order(cart.cart_id, "wrong-token", "k1", NOW)
    first = svc.place_order(cart.cart_id, cart.token, "k1", NOW)
    again = svc.place_order(cart.cart_id, cart.token, "k1", NOW)  # retry
    assert first == again


def test_confirm_cart_rejects_unsafe_cart_even_if_agent_sends_it():
    from foodagent.models import Line
    c = example_constraints()
    b = spice_route_bundle(c).copy()
    b.lines.append(Line(next(i for i in b.restaurant.items if i.name == "Butter Chicken"), 1))
    with pytest.raises(OrderError):
        OrderService().confirm_cart(b, c, NOW)


def test_full_conversation():
    agent = Agent(NOW, use_llm=False)
    reply = agent.handle(EXAMPLE)
    assert "A." in reply and "B." in reply
    letter = "ABC"[[b.restaurant.id for b in agent.options].index("r_spice_route")]
    reply = agent.handle(f"{letter}, but swap naan for more rice")
    assert "Swapped" in reply
    assert "Final check" in agent.handle("confirm")
    assert "Order placed" in agent.handle("yes")
    assert agent.state == "ORDERED"


def test_agent_asks_for_headcount():
    agent = Agent(NOW, use_llm=False)
    assert "How many people" in agent.handle("order dinner")
    assert "A." in agent.handle("6 people, 2 veg, under 2000")

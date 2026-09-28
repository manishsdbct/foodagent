"""Stage 4 bundle builder: a dynamic program over (coverage, cost) per restaurant.

This is a small multiple-choice knapsack. Each eligible dish is a group with choices
qty = 0..q_max; the DP state is how far the bundle is towards each coverage rule, capped
at what the rule needs, so states merge quickly. For each state it keeps a Pareto front
of (subtotal, value): cheaper bundles and better bundles both survive until the end.

Rules built in (design doc, Stage 4):
  quantity      main servings >= headcount and rice/bread servings >= headcount, at most SLACK over
  veg coverage  veg main servings >= vegetarians, and >= 2 distinct veg mains when the menu has them
  non-veg       >= 1 non-veg main for the non-vegetarians when the menu has one
  balance       >= 1 dal or side when the menu has one; <= 40% of spend on starters/desserts
  budget        subtotal x 1.05 + delivery + packaging <= budget

Hard safety filters (allergens, diet, stock) run before this, so every dish it sees is safe.
The greedy builder in engine.py is the fallback for very large groups.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import CARB_COURSES, GST_RATE, MAIN_COURSES, MEAL_COURSES, Bundle, Constraints, Item, Line, Restaurant

FRONT_SIZE = 14          # Pareto points kept per coverage state
MAX_HEADCOUNT = 16       # above this the greedy builder is used instead
SLACK = 2                # at most this many surplus main (and carb) servings: no food waste
EXTRA_SHARE = 0.40       # starters + desserts may take at most this share of the subtotal
LAMBDAS = [0.02, 0.05, 0.10, 0.20, 0.40]  # value-per-₹100 trade-offs used to pick final bundles


def counts_as_veg(item: Item, c: Constraints) -> bool:
    return item.is_veg or (c.allow_egg and item.diet == "egg")


def is_balance(item: Item) -> bool:
    return item.course == "side" or "dal" in item.tags


def is_extra(item: Item) -> bool:
    return item.course in ("starter", "dessert")


@dataclass(frozen=True)
class _Choice:
    prev: "_Choice | None"
    item: int
    qty: int


def _qty_cap(item: Item, hc: int) -> int:
    if item.course in MAIN_COURSES | MEAL_COURSES:
        return max(1, math.ceil(hc / item.serves))
    if item.course in CARB_COURSES:
        return max(1, math.ceil(hc / item.serves))
    if item.course in ("starter", "side", "dessert"):
        return 1 if hc < 6 else 2
    return 0  # beverages are never auto-added


def _unit_value(item: Item, score: float) -> float:
    if item.course in MAIN_COURSES | MEAL_COURSES:
        return score * item.serves
    if item.course in CARB_COURSES:
        return 0.5 * score * item.serves
    return 0.6 * score


def _prune(front: list[tuple[int, float, _Choice | None]]) -> list[tuple[int, float, _Choice | None]]:
    """Keep only points where no cheaper point has at least the same value."""
    front.sort(key=lambda p: (p[0], -p[1]))
    kept, best = [], -1e9
    for p in front:
        if p[1] > best + 1e-9:
            kept.append(p); best = p[1]
    if len(kept) > FRONT_SIZE:  # thin evenly, always keeping the cheapest and the best
        step = (len(kept) - 1) / (FRONT_SIZE - 1)
        kept = [kept[round(i * step)] for i in range(FRONT_SIZE)]
    return kept


def dp_bundles(r: Restaurant, eligible: list[Item], scores: dict[str, float], c: Constraints) -> list[Bundle]:
    """Distinct feasible bundles for one restaurant, one per value/price trade-off in LAMBDAS ([] if none)."""
    hc = c.headcount or 0
    if hc <= 0:
        return []
    veg = c.effective_veg
    items = [i for i in eligible if _qty_cap(i, hc) > 0]
    veg_mains = [i for i in items if counts_as_veg(i, c) and i.main_servings]
    need_vd = min(2, len(veg_mains)) if veg else 0
    need_bal = 1 if any(is_balance(i) for i in items) else 0
    need_nv = 1 if hc > veg and any(i.main_servings and not counts_as_veg(i, c) for i in items) else 0
    h2 = 2 * hc  # half-servings, so 0.5-serving items stay integral
    top = h2 + 2 * SLACK
    v2 = 2 * veg
    fees = r.fees
    cap = math.floor((c.budget_max - fees) / (1 + GST_RATE)) if c.budget_max else None
    if cap is not None and cap <= 0:
        return []

    # state: (main, carb, veg_main, veg_dishes, balance, non_veg) -> Pareto front of (cost, value, choice)
    states: dict[tuple, list] = {(0, 0, 0, 0, 0, 0): [(0, 0.0, None)]}
    for idx, it in enumerate(items):
        m2, k2 = round(2 * it.main_servings), round(2 * it.carb_servings)
        is_v = counts_as_veg(it, c) and it.main_servings > 0
        is_nv = not counts_as_veg(it, c) and it.main_servings > 0
        unit = _unit_value(it, scores[it.id])
        nxt: dict[tuple, list] = {}
        for (m, k, vm, vd, bal, nv), front in states.items():
            for q in range(_qty_cap(it, hc) + 1):
                if q == 0:
                    key = (m, k, vm, vd, bal, nv)
                else:
                    if m + q * m2 > top or k + q * k2 > top:
                        break  # larger q only adds more surplus
                    key = (m + q * m2, k + q * k2,
                           min(v2, vm + q * m2) if is_v else vm,
                           min(need_vd, vd + 1) if is_v else vd,
                           min(need_bal, bal + 1) if is_balance(it) else bal,
                           min(need_nv, nv + 1) if is_nv else nv)
                add_cost = q * it.price
                add_val = (q * unit + 0.25 * scores[it.id]) if q else 0.0  # small bonus per distinct dish
                bucket = nxt.setdefault(key, [])
                for cost, val, ch in front:
                    nc = cost + add_cost
                    if cap is not None and nc > cap:
                        continue
                    bucket.append((nc, val + add_val, _Choice(ch, idx, q) if q else ch))
        states = {k: _prune(v) for k, v in nxt.items() if v}

    finals = [p for (m, k, vm, vd, bal, nv), front in states.items()
              if m >= h2 and k >= h2 and vm >= v2 and vd >= need_vd and bal >= need_bal and nv >= need_nv
              for p in front]
    bundles, seen = [], set()
    for lam in LAMBDAS:
        ranked = sorted(finals, key=lambda p: p[1] - lam * p[0] / 100, reverse=True)
        for cost, _, ch in ranked:
            lines = _lines(ch, items)
            extra = sum(l.item.price * l.qty for l in lines if is_extra(l.item))
            if cost and extra > EXTRA_SHARE * cost:
                continue
            sig = tuple(sorted((l.item.id, l.qty) for l in lines))
            if sig not in seen:
                seen.add(sig)
                bundles.append(Bundle(r, lines))
            break
    return bundles


def _lines(ch: _Choice | None, items: list[Item]) -> list[Line]:
    out = []
    while ch is not None:
        out.append(Line(items[ch.item], ch.qty))
        ch = ch.prev
    order = {"main": 0, "meal": 0, "rice": 1, "noodles": 1, "bread": 2, "side": 3, "starter": 4, "dessert": 5}
    return sorted(reversed(out), key=lambda l: order.get(l.item.course, 9))

"""Matching engine: hard filters -> item scoring -> DP bundle builder -> rank & diversify.

Everything here is deterministic. The agent never computes prices, ETAs or allergen
safety itself; it only presents what these functions return.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import eta as eta_service
from .bundler import MAX_HEADCOUNT, counts_as_veg, dp_bundles
from .models import (CARB_COURSES, EXTRA_COURSES, GST_RATE, MAIN_COURSES, MEAL_COURSES, Bundle,
                     Constraints, Item, Line, Restaurant)

MIN_RATING = 3.8
STRATEGIES = [1.0, 0.7, 0.4, 0.0]  # greedy fallback: weight on quality vs value; lower = cheaper
CROSS_CONTACT_FLAGS = {"shared_fryer_nuts", "cross_contact"}
NUTS = {"peanut", "tree_nut"}


# ---------------------------------------------------------------- time & delivery
def check_deadline(r: Restaurant, now: datetime, c: Constraints) -> tuple[bool, datetime, int | None]:
    e = eta_service.check_eta(r, now, c.deliver_by)
    return e.deadline_ok, e.eta, e.margin_min


def restaurant_block(r: Restaurant, now: datetime, c: Constraints) -> str | None:
    """Stage 1 candidate rules plus the kitchen-level allergy rule."""
    if not r.is_open(now):
        return "closed now"
    if r.rating < MIN_RATING:
        return "rated below 3.8"
    if c.severe and c.allergens & NUTS and set(r.kitchen_flags) & CROSS_CONTACT_FLAGS:
        return "shared fryer / cross-contact kitchen, unsafe for a severe allergy"
    if c.all_veg and not any(i.main_servings and i.is_veg for i in r.items):
        return "no veg mains"
    return None


# ---------------------------------------------------------------- hard filters
def block_reason(item: Item, c: Constraints, strict_search: bool = False) -> str | None:
    """Why an item may not be offered, or None if it is allowed. Safety checks come first."""
    hit = item.allergens() & c.allergens
    if hit:
        return "contains " + ", ".join(sorted(a.replace("_", " ") for a in hit))
    if c.allergens and not item.allergen_verified:
        return "allergen info unverified"
    if not item.in_stock:
        return "out of stock"
    bad = [x for x in c.exclude_ingredients if any(x in ing for ing in item.ingredients)]
    if bad:
        return "contains " + ", ".join(bad)
    if c.all_veg and not counts_as_veg(item, c):
        return "not vegetarian"
    if c.spice_max is not None and item.spice > c.spice_max:
        return "too spicy"
    if strict_search:  # single-dish search: soft wishes become filters
        if c.spice_min and item.course not in CARB_COURSES and item.spice < c.spice_min:
            return "not spicy enough"
        if c.include_ingredients and not set(c.include_ingredients) & set(item.primary):
            return "missing " + ", ".join(c.include_ingredients)
        for taste, level in c.tastes.items():
            if item.tastes.get(taste, 0) < level:
                return f"not {taste} enough"
        if c.region and c.region not in item.famous_in:
            return f"not a {c.region.title()} speciality"
    return None


# ---------------------------------------------------------------- scoring
def score_item(item: Item, r: Restaurant, c: Constraints, profile: dict, max_orders: int) -> float:
    affinity = max((profile.get("cuisine_affinity", {}).get(cu, 0.3) for cu in r.cuisines), default=0.3)
    wishes, hits = 0, 0.0
    is_carb = item.course in CARB_COURSES
    if c.spice_min and not is_carb:
        wishes += 1; hits += min(1.0, item.spice / c.spice_min)
    for taste, level in c.tastes.items():
        wishes += 1; hits += min(1.0, item.tastes.get(taste, 0) / level)
    if c.include_ingredients and not is_carb:
        wishes += 1; hits += 1.0 if set(c.include_ingredients) & set(item.primary) else 0.0
    if c.region:
        wishes += 1; hits += 1.0 if c.region in item.famous_in else 0.0
    if c.cuisines:
        wishes += 1; hits += 1.0 if set(c.cuisines) & set(r.cuisines) else 0.0
    pref = 0.5 * affinity + 0.5 * (hits / wishes if wishes else affinity)
    rating = min(1.0, max(0.0, (item.rating - 3.0) / 2.0))
    popularity = math.log1p(item.orders_30d) / math.log1p(max_orders)
    value = min(1.0, (item.serves * 100 / item.price) / 1.5)
    fit = 1.0 if "shareable" in item.tags or is_carb or item.course in MEAL_COURSES else 0.6
    return round(0.35 * pref + 0.25 * rating + 0.20 * popularity + 0.10 * value + 0.10 * fit, 4)


# ---------------------------------------------------------------- bundle checks
def violations(b: Bundle, c: Constraints) -> list[str]:
    out = []
    for l in b.lines:
        reason = block_reason(l.item, c)
        if reason:
            out.append(f"{l.item.name}: {reason}")
    if c.headcount:
        if b.main_servings < c.headcount:
            out.append(f"only {b.main_servings:g} main servings for {c.headcount} people")
        if b.carb_servings < c.headcount:
            out.append(f"only {b.carb_servings:g} rice/bread servings for {c.headcount} people")
        veg = c.effective_veg
        if veg and b.veg_servings(c.allow_egg) < veg:
            out.append(f"only {b.veg_servings(c.allow_egg):g} veg main servings for {veg} vegetarians")
    if c.budget_max and b.total > c.budget_max:
        out.append(f"₹{b.total - c.budget_max:,.0f} over budget")
    return out


def _qty(lines: dict[str, Line], item: Item, n: int = 1) -> None:
    if item.id in lines:
        lines[item.id].qty += n
    else:
        lines[item.id] = Line(item, n)


def _servings(lines: dict[str, Line], attr: str, veg_only: bool = False) -> float:
    return sum(getattr(l.item, attr) * l.qty for l in lines.values() if l.item.is_veg or not veg_only)


def greedy_bundle(r: Restaurant, eligible: list[Item], scores: dict[str, float], c: Constraints, alpha: float) -> Bundle:
    """Fill veg coverage, then mains, then carbs, then optional extras, ordered by a quality/value blend."""
    def key(i: Item) -> float:
        value = min(1.0, (i.serves * 100 / i.price) / 1.5)
        return alpha * scores[i.id] + (1 - alpha) * value

    hc, veg = c.headcount, c.effective_veg
    mains = sorted([i for i in eligible if i.course in MAIN_COURSES | MEAL_COURSES], key=key, reverse=True)
    carbs = sorted([i for i in eligible if i.course in CARB_COURSES], key=key, reverse=True)
    veg_mains = [i for i in mains if counts_as_veg(i, c)]
    lines: dict[str, Line] = {}

    # 1. Vegetarians: two distinct veg dishes when possible, enough servings for all of them
    if veg:
        for it in veg_mains[:2]:
            _qty(lines, it)
        k = 0
        while veg_mains and _servings(lines, "main_servings", veg_only=True) < veg:
            _qty(lines, veg_mains[k % min(2, len(veg_mains))]); k += 1

    # 2. Mains for everyone; give non-veg eaters one dish of their own when there is one
    non_veg = [i for i in mains if not i.is_veg]
    if hc > veg and non_veg and not any(not l.item.is_veg for l in lines.values()):
        _qty(lines, non_veg[0])
    k = 0
    while mains and _servings(lines, "main_servings") < hc:
        fresh = [i for i in mains if i.id not in lines]
        if fresh and len(lines) < math.ceil(hc / 2) + 1:
            _qty(lines, fresh[0])
        else:
            picked = [i for i in mains if i.id in lines]
            _qty(lines, picked[k % len(picked)]); k += 1

    # 3. Rice / bread: split between the best rice and the best bread when both exist
    gap = hc - _servings(lines, "carb_servings")
    if gap > 0 and carbs:
        rice = next((i for i in carbs if i.course in ("rice", "noodles")), None)
        bread = next((i for i in carbs if i.course == "bread"), None)
        if rice and bread and gap >= 4:
            n_rice = math.ceil((gap / 2) / rice.serves)
            _qty(lines, rice, n_rice)
            rest = gap - n_rice * rice.serves
            if rest > 0:
                _qty(lines, bread, math.ceil(rest / bread.serves))
        else:
            _qty(lines, carbs[0], math.ceil(gap / carbs[0].serves))

    bundle = Bundle(r, list(lines.values()))
    cap = (c.budget_max * 0.97) if c.budget_max else None

    def fits(extra_price: int) -> bool:
        return cap is None or (bundle.subtotal + extra_price) * (1 + GST_RATE) + r.fees <= cap

    # 3b. Spare budget: one more main dish so a group of 4+ gets real choice
    if cap and hc >= 4:
        fresh = [i for i in mains if i.id not in lines]
        if fresh and fits(fresh[0].price) and bundle.subtotal * (1 + GST_RATE) + r.fees < 0.75 * c.budget_max:
            bundle.lines.append(Line(fresh[0], 1))

    # 4. Extras (starter / side / dessert) only while comfortably inside the budget
    veg_boost = 0.15 if veg and bundle.veg_main_dishes < 2 else 0.0  # few veg mains -> prefer veg extras
    extras = sorted([i for i in eligible if i.course in EXTRA_COURSES - {"beverage"}],
                    key=lambda i: key(i) + (veg_boost if i.is_veg else 0), reverse=True)
    added_courses: set[str] = set()
    for it in extras:
        if len(added_courses) >= 2 or it.course in added_courses:
            continue
        if fits(it.price):
            bundle.lines.append(Line(it, 1)); added_courses.add(it.course)
    return bundle


def bundle_score(b: Bundle, scores: dict[str, float], c: Constraints) -> float:
    weight = sum(l.qty for l in b.lines)
    s = sum(scores[l.item.id] * l.qty for l in b.lines) / weight
    s += 0.02 * min(len(b.lines), 5)  # variety
    if c.headcount:  # coverage bonus: every veg diner gets a choice of dishes
        s *= 1.0 + (0.05 if b.veg_main_dishes >= 2 or not c.effective_veg else 0.0)
    if b.margin_min is not None and b.margin_min < 15:
        s -= 0.08  # ETA close to the deadline
    if c.budget_max:
        s -= 0.10 * max(0.0, b.total / c.budget_max - 0.75) / 0.25  # spend close to the cap
    return round(s, 4)


def reason_codes(b: Bundle, c: Constraints) -> list[str]:
    """Machine reason codes the agent turns into one plain sentence (design doc, Stage 5)."""
    codes = []
    if c.budget_max:
        codes.append(f"under_budget_by_{int(c.budget_max - b.total)}")
    if b.margin_min is not None:
        codes.append(f"eta_margin_{b.margin_min}m")
    if "nut_free_kitchen" in b.restaurant.kitchen_flags:
        codes.append("nut_free_kitchen")
    if b.restaurant.pure_veg:
        codes.append("pure_veg_kitchen")
    if c.allergens & NUTS and any("tree nut" in e for e in b.excluded):
        codes.append("cashew_gravies_excluded")
    if c.allergens & {"peanut"} and any("peanut" in e for e in b.excluded):
        codes.append("peanut_dishes_excluded")
    if c.effective_veg and not c.all_veg:
        codes.append(f"veg_dishes_{b.veg_main_dishes}")
    return codes


def explain(b: Bundle, c: Constraints) -> list[str]:
    reasons = []
    if c.budget_max:
        reasons.append(f"₹{c.budget_max - b.total:,.0f} under budget")
    if b.margin_min is not None:
        reasons.append(f"arrives ~{b.eta:%H:%M}, {b.margin_min} min before your deadline")
    veg = c.effective_veg
    if veg and not c.all_veg:
        n = b.veg_main_dishes
        reasons.append(f"{n} veg {'dish' if n == 1 else 'dishes'} for {veg} vegetarian{'s' if veg > 1 else ''}")
    if c.allergens:
        free = ", ".join(sorted(a.replace("_", " ") for a in c.allergens))
        tag = " (nut-free kitchen)" if "nut_free_kitchen" in b.restaurant.kitchen_flags else ""
        reasons.append(f"every dish is free of {free}{tag}")
    return reasons


# ---------------------------------------------------------------- public API
@dataclass
class Recommendation:
    bundles: list[Bundle] = field(default_factory=list)
    near_misses: list[tuple[str, str, float | None]] = field(default_factory=list)  # (restaurant, problem, total)


def _build(r: Restaurant, eligible: list[Item], scores: dict[str, float], c: Constraints) -> list[Bundle]:
    """DP bundles first; the greedy strategies are the fallback (very large groups, or DP finds none)."""
    if (c.headcount or 0) <= MAX_HEADCOUNT:
        found = [b for b in dp_bundles(r, eligible, scores, c) if b.lines]
        if found:
            return found
    out, seen = [], set()
    for alpha in STRATEGIES:
        b = greedy_bundle(r, eligible, scores, c, alpha)
        sig = tuple(sorted((l.item.id, l.qty) for l in b.lines))
        if b.lines and sig not in seen:
            seen.add(sig); out.append(b)
    return out


def recommend_bundles(c: Constraints, restaurants: list[Restaurant], now: datetime, profile: dict, k: int = 3) -> Recommendation:
    rec = Recommendation()
    if not c.headcount:  # a meal bundle needs a headcount; the agent asks for it first
        return rec
    max_orders = max(i.orders_30d for r in restaurants for i in r.items)
    per_restaurant: list[list[Bundle]] = []
    for r in restaurants:
        if restaurant_block(r, now, c):
            continue
        ok, arrive, margin = check_deadline(r, now, c)
        if not ok:
            buf = eta_service.buffer_min(arrive)
            rec.near_misses.append((r.name, f"earliest arrival ~{arrive:%H:%M} (+{buf} min buffer) misses {c.deliver_by:%H:%M}", None))
            continue
        eligible, excluded = [], []
        for it in r.items:
            why = block_reason(it, c)
            if why:
                if c.allergens and ("contains" in why or "unverified" in why):
                    excluded.append(f"{it.name} ({why})")
            else:
                eligible.append(it)
        scores = {i.id: score_item(i, r, c, profile, max_orders) for i in eligible}
        feasible, best_miss = [], None
        for b in _build(r, eligible, scores, c):
            b.eta, b.margin_min, b.excluded = arrive, margin, excluded
            problems = violations(b, c)
            if problems:
                if best_miss is None or b.total < best_miss[2]:
                    best_miss = (r.name, "; ".join(problems), b.total)
                continue
            b.score = bundle_score(b, scores, c)
            b.reasons = reason_codes(b, c)
            feasible.append(b)
        if feasible:
            per_restaurant.append(sorted(feasible, key=lambda x: x.score, reverse=True)[:2])  # best 2 per restaurant
        elif best_miss:
            rec.near_misses.append(best_miss)
    rec.bundles = diversify(per_restaurant, k)
    for n, b in enumerate(rec.bundles, 1):
        b.bundle_id = f"b_{n}"
    rec.near_misses.sort(key=lambda m: (m[2] is None, m[2] or 0))
    return rec


def diversify(per_restaurant: list[list[Bundle]], k: int) -> list[Bundle]:
    """Top k with different restaurants and at least two cuisines where possible.

    A restaurant's second-best bundle is only used when there are fewer than k restaurants.
    """
    firsts = sorted((opts[0] for opts in per_restaurant), key=lambda b: b.score, reverse=True)
    picked = firsts[:k]
    cuisines = {cu for b in picked for cu in b.restaurant.cuisines}
    if len(picked) == k and len(cuisines) < 2:
        other = next((b for b in firsts[k:] if set(b.restaurant.cuisines) - cuisines), None)
        if other:
            picked[-1] = other
    if len(picked) < k:
        seconds = sorted((opts[1] for opts in per_restaurant if len(opts) > 1), key=lambda b: b.score, reverse=True)
        picked += seconds[:k - len(picked)]
        picked.sort(key=lambda b: b.score, reverse=True)
    return picked


@dataclass
class DishHit:
    item: Item
    restaurant: Restaurant
    score: float
    eta: datetime


def search_dishes(c: Constraints, restaurants: list[Restaurant], now: datetime, profile: dict, k: int = 5) -> list[DishHit]:
    """Single-dish search: 'suggest me spicy paneer option', 'something sweet famous in Delhi'."""
    max_orders = max(i.orders_30d for r in restaurants for i in r.items)
    hits = []
    for r in restaurants:
        if restaurant_block(r, now, c):
            continue
        ok, arrive, _ = check_deadline(r, now, c)
        if not ok:
            continue
        for it in r.items:
            if c.cuisines and not set(c.cuisines) & set(r.cuisines):
                continue
            if block_reason(it, c, strict_search=True) is None and it.course not in CARB_COURSES:
                hits.append(DishHit(it, r, score_item(it, r, c, profile, max_orders), arrive))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


# ---------------------------------------------------------------- edits
def _find(r: Restaurant, phrase: str) -> Item | None:
    phrase = re.sub(r"\b(the|some|more|extra|a|an|please|instead)\b", " ", phrase.lower()).strip(" .,!")
    if not phrase:
        return None
    for it in r.items:  # substring match on name words first ("naan" -> "Butter Naan")
        if phrase in it.name.lower() or it.name.lower() in phrase:
            return it
    words = [w for w in phrase.split() if len(w) > 2 or w.isdigit()]
    for it in r.items:  # every meaningful word must appear ("chicken 65" must not match "Butter Chicken")
        name_words = re.findall(r"[a-z0-9]+", it.name.lower())
        if words and all(any(nw.startswith(w) for nw in name_words) for w in words):
            return it
    names = {it.name.lower(): it for it in r.items}
    close = difflib.get_close_matches(phrase, names, n=1, cutoff=0.7)  # typos: "jeera rce"
    return names[close[0]] if close else None


def modify_bundle(b: Bundle, text: str, c: Constraints) -> tuple[Bundle, str]:
    """Apply one edit in plain words. Returns (bundle, message). Unsafe edits are refused."""
    t = text.lower().strip()
    new = b.copy()
    r = new.restaurant

    def line_for(item: Item) -> Line | None:
        return next((l for l in new.lines if l.item.id == item.id), None)

    def refuse_if_unsafe(item: Item) -> str | None:
        why = block_reason(item, c)
        return f"I can't add {item.name}: {why}." if why else None

    m = re.search(r"(?:swap|replace|change)\s+(?:the\s+)?(.+?)\s+(?:for|with|to)\s+(.+)", t)
    if m:
        old_item, new_item = _find(r, m.group(1)), _find(r, m.group(2))
        if not old_item or not line_for(old_item):
            return b, f"I couldn't find \"{m.group(1)}\" in this order."
        if not new_item:
            return b, f"{r.name} doesn't have \"{m.group(2)}\"."
        if (msg := refuse_if_unsafe(new_item)):
            return b, msg
        new.lines.remove(line_for(old_item))
        role = "carb_servings" if old_item.carb_servings and not old_item.main_servings else "main_servings"
        needed = (c.headcount or 0) - sum(getattr(l.item, role) * l.qty for l in new.lines)
        per = getattr(new_item, role) or new_item.serves
        add = max(1, math.ceil(needed / per)) if needed > 0 else max(1, math.ceil(old_item.serves * line_for_qty(b, old_item) / new_item.serves))
        existing = line_for(new_item)
        if existing:
            existing.qty += add
        else:
            new.lines.append(Line(new_item, add))
        return new, f"Swapped {old_item.name} for {add} more {new_item.name}."

    m = re.search(r"(?:remove|drop|no|without|skip)\s+(?:the\s+)?(.+)", t)
    if m:
        it = _find(r, m.group(1))
        if not it or not line_for(it):
            return b, f"I couldn't find \"{m.group(1)}\" in this order."
        new.lines.remove(line_for(it))
        return new, f"Removed {it.name}."

    m = re.search(r"(?:add|plus|include)\s+(\d+|one|two|three|four|an?)?\s*(?:x\s+)?(?:more\s+)?(.+)", t)
    if m:
        it = _find(r, m.group(2))
        if not it:
            return b, f"{r.name} doesn't have \"{m.group(2)}\"."
        if (msg := refuse_if_unsafe(it)):
            return b, msg
        n = {"one": 1, "two": 2, "three": 3, "four": 4, "a": 1, "an": 1}.get(m.group(1) or "", None) or int(m.group(1) or 1)
        existing = line_for(it)
        if existing:
            existing.qty += n
        else:
            new.lines.append(Line(it, n))
        return new, f"Added {n}× {it.name}."

    m = re.search(r"(?:make|set)\s+(.+?)\s+(?:to|x|×)\s*(\d+)", t)
    if m:
        it = _find(r, m.group(1))
        if not it or not line_for(it):
            return b, f"I couldn't find \"{m.group(1)}\" in this order."
        line_for(it).qty = int(m.group(2))
        if line_for(it).qty == 0:
            new.lines.remove(line_for(it))
        return new, f"Set {it.name} to {m.group(2)}."

    return b, "I didn't catch that edit. Try: swap naan for rice · remove raita · add 2 roti · make rice 3."


def substitute_for(item: Item, r: Restaurant, c: Constraints) -> Item | None:
    """Closest safe replacement from the same restaurant: same course and diet class, nearest price."""
    options = [i for i in r.items if i.id != item.id and i.course == item.course
               and i.is_veg == item.is_veg and block_reason(i, c) is None]
    options.sort(key=lambda i: (not set(i.primary) & set(item.primary), abs(i.price - item.price)))
    return options[0] if options else None


def line_for_qty(b: Bundle, item: Item) -> int:
    return next((l.qty for l in b.lines if l.item.id == item.id), 1)

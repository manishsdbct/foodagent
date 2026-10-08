"""The six agent tools (design doc, "Agent tools (API contracts)"), JSON in and JSON out.

Only place_order has side effects, and it needs the confirm_token issued by confirm_cart plus an
explicit "yes" from the customer in a later turn than the one that showed the final summary.

Safety does not depend on the LLM: every allergen the rule parser finds in any customer
message is merged into the constraints before the engine runs, and confirm_cart re-checks the
cart against those merged constraints.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from . import engine
from .engine import check_deadline, explain, substitute_for, violations
from .eta import check_eta as eta_for
from .models import Bundle, Constraints, Restaurant
from .orders import Cart, OrderError, OrderService, PaymentDeclined
from .schema import OrderConstraints

YES = re.compile(r"^\s*(y|yes|yeah|yep|ok|okay|sure|confirm|place( it| the order)?|go ahead|do it|haan|ha)\b", re.I)
NOT_YET = re.compile(r"\b(but|change|swap|add|remove|instead|wait|no|not)\b", re.I)
ALLERGY_NOTE = "Allergen data comes from the restaurant; the allergy has been added to the order note."
METHOD_NOISE = {"saved", "ending", "with", "the", "my"}


def mentioned_method(text: str, methods: list[str]) -> str | None:
    """The saved payment method a message names ("use the card", "UPI"), if exactly one matches."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    hits = [m for m in methods if words & (set(re.findall(r"[a-z0-9]+", m.lower())) - METHOD_NOISE)]
    return hits[0] if len(hits) == 1 else None


class ToolError(Exception):
    pass


@dataclass
class Session:
    """Per-chat memory: constraints, shortlisted bundles, cart (design doc: Redis, 2 h TTL)."""
    now: datetime
    restaurants: list[Restaurant]
    profile: dict
    orders: OrderService
    constraints: Constraints | None = None
    declared_allergens: set[str] = field(default_factory=set)   # union over every customer message
    severe: bool = False
    bundles: dict[str, Bundle] = field(default_factory=dict)
    cart: Cart | None = None
    cart_turn: int = -1
    turn: int = 0
    user_text: str = ""
    state: str = "GATHERING"
    trace: list[dict] = field(default_factory=list)
    trace_path: Path | None = None
    payment_declined: str | None = None  # method that just failed, so "yes, use the card" can retry

    def new_turn(self, text: str, rule_fields: dict) -> None:
        self.turn += 1
        self.user_text = text
        self.declared_allergens |= set(rule_fields.get("allergens", set()))
        self.severe = self.severe or bool(rule_fields.get("severe"))


# ---------------------------------------------------------------- serialisation
def bundle_json(b: Bundle, c: Constraints) -> dict:
    r = b.restaurant
    return {
        "bundle_id": b.bundle_id, "restaurant": r.name, "restaurant_id": r.id, "cuisines": r.cuisines,
        "items": [{"name": l.item.name, "qty": l.qty, "veg": l.item.is_veg, "diet": l.item.diet,
                   "course": l.item.course, "serves_each": l.item.serves, "line_total": l.item.price * l.qty}
                  for l in b.lines],
        "price": {"subtotal": b.subtotal, "gst": b.gst, "delivery": r.delivery_fee, "packaging": r.packaging_fee,
                  "total": round(b.total, 2), "total_display": round(b.total)},
        "eta": f"{b.eta:%H:%M}" if b.eta else None, "deadline_margin_min": b.margin_min,
        "coverage": {"main_servings": b.main_servings, "carb_servings": b.carb_servings,
                     "veg_dishes": b.veg_main_dishes,
                     "allergen_free": sorted(c.allergens), "allergen_scope": "whole_bundle" if c.allergens else None},
        "reasons": b.reasons, "reason_text": explain(b, c),
        "left_out_for_safety": b.excluded[:6],
    }


# ---------------------------------------------------------------- tools
class Tools:
    def __init__(self, session: Session):
        self.s = session

    def call(self, name: str, args: dict) -> dict:
        """Run one tool; errors come back as {"error": ...} so the agent can explain them.
        An unexpected failure (database, network) is retried once, then reported as temporary;
        place_order is idempotent, so the retry cannot double-charge."""
        fn = getattr(self, name) if name in TOOL_NAMES else None
        for attempt in (1, 2):
            try:
                if fn is None:
                    raise ToolError(f"unknown tool {name}")
                out = fn(**args)
            except (ToolError, OrderError) as e:
                out = {"error": str(e)}
            except (TypeError, ValidationError) as e:
                out = {"error": f"invalid input: {e}"}
            except Exception as e:
                if attempt == 1:
                    continue
                out = {"error": f"{name} is temporarily unavailable ({type(e).__name__}). The request did not complete; "
                                "tell the customer plainly, keep their choices, and offer to try again.",
                       "temporary": True}
            break
        self._trace(name, args, out)
        return out

    def _trace(self, name: str, args: dict, out: dict) -> None:
        row = {"turn": self.s.turn, "tool": name, "input": args,
               "bundle_ids": [b.get("bundle_id") for b in out.get("bundles", [])] or out.get("bundle", {}).get("bundle_id"),
               "error": out.get("error")}
        self.s.trace.append(row)
        if self.s.trace_path:
            with open(self.s.trace_path, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")

    def _constraints(self, raw: dict | None) -> Constraints:
        if raw is None:
            if not self.s.constraints:
                raise ToolError("no constraints yet; call recommend_bundles first")
            return self.s.constraints
        c = OrderConstraints.model_validate(raw).to_constraints(self.s.now)
        c.allergens |= self.s.declared_allergens  # the LLM can never drop an allergy the customer typed
        c.severe = c.severe or self.s.severe
        return c

    def _bundle(self, bundle_id: str) -> Bundle:
        b = self.s.bundles.get(bundle_id)
        if not b:
            raise ToolError(f"unknown bundle_id {bundle_id}; valid: {sorted(self.s.bundles)}")
        return b

    # 1
    def get_user_context(self, user_id: str = "me") -> dict:
        p = self.s.profile
        return {"user_id": p.get("id"), "name": p.get("name"), "default_address": p.get("default_address"),
                "addresses": p.get("addresses", []), "saved_allergies": p.get("saved_allergies", []),
                "spice_pref": p.get("spice_pref"), "cuisine_affinity": p.get("cuisine_affinity", {}),
                "recent_orders": p.get("recent_orders", []), "payment_methods": p.get("payment_methods", []),
                "local_time": f"{self.s.now:%Y-%m-%d %H:%M}"}

    # 2
    def recommend_bundles(self, constraints: dict, k: int = 3) -> dict:
        c = self._constraints(constraints)
        if not c.headcount:
            raise ToolError("headcount is required; ask the customer how many people are eating")
        if c.address_id and c.address_id not in {a["id"] for a in self.s.profile.get("addresses", [])}:
            raise ToolError(f"unknown address_id {c.address_id}")
        self.s.constraints = c
        rec = engine.recommend_bundles(c, self.s.restaurants, self.s.now, self.s.profile, k=min(max(k, 1), 5))
        self.s.bundles = {b.bundle_id: b for b in rec.bundles}
        self.s.cart = None
        self.s.state = "RECOMMENDING" if rec.bundles else "NEGOTIATING"
        out = {"constraints_used": OrderConstraints.from_constraints(c).model_dump(exclude_none=True),
               "bundles": [bundle_json(b, c) for b in rec.bundles],
               "near_misses": [{"restaurant": n, "breaks": why, "total": round(t) if t else None}
                               for n, why, t in rec.near_misses[:2 if rec.bundles else 3]]}
        if rec.unmet:
            out["unmet_request"] = rec.unmet
            out["other_cuisines_open_now"] = sorted({cu for r in self.s.restaurants if not engine.restaurant_block(r, self.s.now, c)
                                                     for cu in r.cuisines} - set(c.cuisines))
            out["next_step"] = ("The customer's cuisine or dish request can't be met. Say so plainly with the reason "
                                "(near_misses), do not offer other food in its place, and ask whether to relax a "
                                "constraint or choose another cuisine (other_cuisines_open_now).")
        if c.allergens:
            out["allergen_note"] = ALLERGY_NOTE
            if c.allergens >= {"peanut", "tree_nut"}:
                out["assumption"] = "A generic nut allergy is treated as both peanuts and tree nuts; say so and offer to narrow it."
        return out

    # 3
    def modify_bundle(self, bundle_id: str, edits: list[dict]) -> dict:
        c = self._constraints(None)
        orig = b = self._bundle(bundle_id)
        messages = []
        for e in edits:
            op, item = e.get("op"), e.get("item", "")
            qty = int(e.get("qty", 1))
            text = {"add": f"add {qty} {item}", "remove": f"remove {item}",
                    "swap": f"swap {item} for {e.get('with', '')}", "set_qty": f"make {item} to {qty}"}.get(op)
            if not text:
                raise ToolError(f"unknown edit op {op!r}; use add, remove, swap or set_qty")
            new, msg = engine.modify_bundle(b, text, c)
            if new is b:  # refused or not understood: nothing from this call is applied
                return {"applied": False, "message": msg, "bundle": bundle_json(orig, c)}
            b = new
            messages.append(msg)
        hard = [p for p in violations(b, c) if "over budget" in p or "contains" in p or "unverified" in p]
        if hard:
            return {"applied": False, "message": " ".join(messages), "breaks": hard,
                    "bundle": bundle_json(orig, c)}
        b.reasons = [r for r in orig.reasons if not r.startswith("under_budget")]
        if c.budget_max:
            b.reasons.insert(0, f"under_budget_by_{int(c.budget_max - b.total)}")
        self.s.bundles[bundle_id] = b
        self.s.cart = None
        self.s.state = "EDITING"
        return {"applied": True, "message": " ".join(messages),
                "warnings": [p for p in violations(b, c) if "servings" in p], "bundle": bundle_json(b, c)}

    # 4
    def check_eta(self, restaurant_id: str, address_id: str | None = None) -> dict:
        r = next((r for r in self.s.restaurants if r.id == restaurant_id), None)
        if not r:
            raise ToolError(f"unknown restaurant_id {restaurant_id}")
        deadline = self.s.constraints.deliver_by if self.s.constraints else None
        return {**eta_for(r, self.s.now, deadline).to_dict(), "address_id": address_id or self.s.profile.get("default_address")}

    # 5
    def confirm_cart(self, bundle_id: str) -> dict:
        c = self._constraints(None)
        b = self._bundle(bundle_id)
        stock = [l for l in b.lines if not l.item.in_stock]
        if stock:
            subs = {l.item.name: (s.name if (s := substitute_for(l.item, b.restaurant, c)) else None) for l in stock}
            raise ToolError(f"out of stock: {', '.join(subs)}. Closest safe substitutes: {subs}. Use modify_bundle, then confirm again.")
        ok, arrive, margin = check_deadline(b.restaurant, self.s.now, c)
        if not ok:
            raise ToolError(f"{b.restaurant.name} can no longer arrive before {c.deliver_by:%H:%M} (ETA {arrive:%H:%M})")
        b.eta, b.margin_min = arrive, margin
        cart = self.s.orders.confirm_cart(b, c, self.s.now)  # re-runs every hard check server-side
        self.s.cart, self.s.cart_turn = cart, self.s.turn
        self.s.state = "CONFIRMING"
        return {"cart_id": cart.cart_id, "bundle_id": bundle_id, "restaurant": b.restaurant.name,
                "items": bundle_json(b, c)["items"], "price": cart.price_breakdown(),
                "eta": f"{b.eta:%H:%M}", "confirm_token": cart.token, "token_expires": f"{cart.expires:%H:%M}",
                "order_note": cart.note, "payment_methods": self.s.profile.get("payment_methods", []),
                "next_step": "Show this summary and ask for an explicit yes. Do not call place_order in this turn."}

    # 6
    def place_order(self, cart_id: str, confirm_token: str, idempotency_key: str, payment_method: str = "saved UPI") -> dict:
        cart = self.s.cart
        if not cart or cart.cart_id != cart_id:
            raise ToolError("no confirmed cart with that id; call confirm_cart first")
        if self.s.turn <= self.s.cart_turn:
            raise ToolError("the customer has not seen the final summary yet; ask for an explicit yes first")
        methods = self.s.profile.get("payment_methods") or [payment_method]
        if payment_method not in methods:
            raise ToolError(f"unknown payment_method {payment_method!r}; saved methods: {methods}")
        switching = self.s.payment_declined and mentioned_method(self.s.user_text, methods) == payment_method
        if not YES.match(self.s.user_text) or (NOT_YET.search(self.s.user_text) and not switching):
            raise ToolError("the customer's latest message is not an explicit yes; ask them to confirm")
        try:
            order = self.s.orders.place_order(cart_id, confirm_token, idempotency_key, self.s.now, payment_method)
        except PaymentDeclined as e:
            self.s.payment_declined = e.method
            return {"error": str(e), "payment_failed": True, "cart_held_until": f"{e.held_until:%H:%M}",
                    "other_payment_methods": [m for m in methods if m != e.method],
                    "next_step": "Tell the customer the payment did not go through and the cart is held; offer another "
                                 "saved method and place the order with it only after an explicit yes."}
        self.s.payment_declined = None
        self.s.state = "ORDERED"
        return order


TOOL_NAMES = ["get_user_context", "recommend_bundles", "modify_bundle", "check_eta", "confirm_cart", "place_order"]


def tool_definitions() -> list[dict]:
    """Tool definitions passed to the model. OrderConstraints comes from the Pydantic schema."""
    schema = OrderConstraints.model_json_schema()
    defs = schema.pop("$defs", {})
    return [
        {"name": "get_user_context",
         "description": "Saved addresses, allergies, taste preferences, recent orders and payment methods for the customer, plus the local time. Call once at the start of a conversation to fill gaps without asking.",
         "input_schema": {"type": "object", "properties": {"user_id": {"type": "string", "default": "me"}}}},
        {"name": "recommend_bundles",
         "description": "Return up to k complete meal bundles that satisfy ALL hard constraints (headcount, diet, allergens, budget incl. tax+fees, delivery deadline). If none fit, returns near_misses with the constraint each one breaks. Never invent items or prices; only present what this tool returns.",
         "input_schema": {"type": "object", "$defs": defs,
                          "properties": {"constraints": schema,
                                         "k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 5}},
                          "required": ["constraints"]}},
        {"name": "modify_bundle",
         "description": "Apply edits to a shortlisted bundle: add, remove, swap (item -> with) or set_qty. Re-prices and re-runs every allergen, diet and budget check; unsafe edits are refused. Returns the updated bundle.",
         "input_schema": {"type": "object", "properties": {
             "bundle_id": {"type": "string"},
             "edits": {"type": "array", "items": {"type": "object", "properties": {
                 "op": {"type": "string", "enum": ["add", "remove", "swap", "set_qty"]},
                 "item": {"type": "string", "description": "Dish name as the customer said it"},
                 "with": {"type": "string", "description": "For swap: the replacement dish"},
                 "qty": {"type": "integer", "minimum": 0}}, "required": ["op", "item"]}}},
             "required": ["bundle_id", "edits"]}},
        {"name": "check_eta",
         "description": "Delivery estimate for one restaurant: eta, eta_min, eta_max and whether it meets the current deadline with the safety buffer.",
         "input_schema": {"type": "object", "properties": {"restaurant_id": {"type": "string"}, "address_id": {"type": "string"}},
                          "required": ["restaurant_id"]}},
        {"name": "confirm_cart",
         "description": "Hold the chosen bundle as a cart: re-checks every rule, returns the final price breakdown, ETA and a confirm_token valid for 5 minutes. Show the summary and wait for the customer's explicit yes.",
         "input_schema": {"type": "object", "properties": {"bundle_id": {"type": "string"}}, "required": ["bundle_id"]}},
        {"name": "place_order",
         "description": "Place and pay for a confirmed cart. Only call after the customer explicitly said yes to the confirm_cart summary in their latest message. Side effect: charges the customer.",
         "input_schema": {"type": "object", "properties": {
             "cart_id": {"type": "string"}, "confirm_token": {"type": "string"},
             "payment_method": {"type": "string", "default": "saved UPI"},
             "idempotency_key": {"type": "string", "description": "Any unique string; reuse it when retrying the same order"}},
             "required": ["cart_id", "confirm_token", "idempotency_key"]}},
    ]

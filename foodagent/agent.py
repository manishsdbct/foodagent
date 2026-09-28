"""Conversation state machine: GATHERING -> RECOMMENDING -> EDITING -> CONFIRMING -> ORDERED."""
from __future__ import annotations

import math
import re
import secrets
from datetime import datetime

from .engine import DishHit, modify_bundle, recommend_bundles, search_dishes, violations, check_deadline
from .models import Bundle, Constraints, Line, load_data
from .orders import OrderError, OrderService, PaymentDeclined
from .parser import parse
from .tools import mentioned_method

YES = r"^(y|yes|yeah|yep|ok|okay|sure|confirm|place( it| the order)?|go ahead|do it|haan|ha)\b"
DONE = r"\b(confirm|checkout|check out|looks good|that'?s it|done|order it|place (it|the order|order))\b"
LETTERS = "ABC"


def money(x: float) -> str:
    return f"₹{x:,.0f}"


class Agent:
    def __init__(self, now: datetime, use_llm: bool = True, orders: OrderService | None = None):
        self.restaurants, self.profile = load_data()
        self.now = now
        self.use_llm = use_llm
        self.orders = orders or OrderService()
        self.reset()

    def reset(self) -> None:
        self.state = "GATHERING"
        self.c = Constraints()
        self.options: list[Bundle] = []
        self.dish_hits: list[DishHit] = []
        self.bundle: Bundle | None = None
        self.cart = None
        self.idem_key = secrets.token_hex(8)
        self.payment = (self.profile.get("payment_methods") or ["saved UPI"])[0]

    # ------------------------------------------------------------ entry point
    def handle(self, text: str) -> str:
        t = text.strip()
        low = t.lower()
        if re.match(r"^(restart|start over|new order|reset)\b", low):
            self.reset()
            return "Starting fresh. What would you like to order?"
        if self.state == "ORDERED":
            self.reset()
        if self.state == "CONFIRMING":
            return self._confirming(low)
        if self.state == "EDITING":
            return self._editing(t)
        if self.state == "RECOMMENDING":
            picked = self._pick(low)
            if picked:
                return picked
        return self._gather(t)

    # ------------------------------------------------------------ states
    def _gather(self, text: str) -> str:
        fields, source = parse(text, self.now, self.use_llm)
        self.c.update(fields)
        wants_meal = bool(re.search(r"\b(order|dinner|lunch|breakfast|party|meal|people|guests)\b", text.lower()))
        if wants_meal and not self.c.headcount:
            self.state = "GATHERING"
            return "Happy to help. How many people are eating?"
        if self.c.mode == "dishes":
            return self._show_dishes()
        return self._show_bundles()

    def _show_dishes(self) -> str:
        self.dish_hits = search_dishes(self.c, self.restaurants, self.now, self.profile)
        if not self.dish_hits:
            return f"I couldn't find dishes matching {self.c.summary()}. Try relaxing one filter."
        self.state = "RECOMMENDING"
        out = [f"Dishes for: {self.c.summary()}"] + self._notes()
        for n, h in enumerate(self.dish_hits, 1):
            it = h.item
            veg = "veg" if it.is_veg else ("egg" if it.diet == "egg" else "non-veg")
            fame = f" · famous in {', '.join(x.title() for x in it.famous_in)}" if it.famous_in else ""
            out.append(f"  {n}. {it.name} — {h.restaurant.name} · {money(it.price)} (serves {it.serves:g}) · {veg} · "
                       f"spice {it.spice}/4 · ⭐ {it.rating}{fame} · ETA ~{h.eta:%H:%M}")
        out.append("Reply with a number to start an order (e.g. \"add 2\"), or refine: \"less spicy\", \"under 250\".")
        return "\n".join(out)

    def _show_bundles(self) -> str:
        rec = recommend_bundles(self.c, self.restaurants, self.now, self.profile)
        self.options = rec.bundles
        out = [f"Looking for: {self.c.summary()}"] + self._notes()
        if not rec.bundles:
            self.state = "GATHERING"
            out.append("Nothing fits every constraint. Closest options:")
            for name, problem, total in rec.near_misses[:3]:
                out.append(f"  • {name}: {problem}" + (f" (total {money(total)})" if total else ""))
            out.append("Tell me what to relax, e.g. \"budget 2300\" or \"by 8:30pm\".")
            return "\n".join(out)
        self.state = "RECOMMENDING"
        for letter, b in zip(LETTERS, rec.bundles):
            out.append("")
            out.append(self._render(b, f"{letter}. "))
        letter, b = LETTERS[len(rec.bundles) - 1], rec.bundles[-1]  # example edit built from a real bundle
        out.append(f"\nPick {', '.join(LETTERS[:len(rec.bundles)])} — you can add an edit too, "
                   f"e.g. \"{letter}, but remove {b.lines[-1].item.name}\".")
        return "\n".join(out)

    def _pick(self, low: str) -> str | None:
        if self.dish_hits:
            m = re.match(r"^(?:add|order|pick|option|#)?\s*(\d)\b", low)
            if m and 1 <= int(m.group(1)) <= len(self.dish_hits):
                h = self.dish_hits[int(m.group(1)) - 1]
                qty = math.ceil((self.c.headcount or 1) / h.item.serves)
                _, arrive, margin = check_deadline(h.restaurant, self.now, self.c)
                self.bundle = Bundle(h.restaurant, [Line(h.item, qty)], arrive, margin)
                self.state = "EDITING"
                return self._render(self.bundle, "Your order: ") + "\nAdd more from this restaurant, or say \"confirm\"."
            return None
        m = re.match(r"^(?:option\s*|pick\s*|go with\s*)?([abc])\b[\s,.:;-]*(?:but\s+)?(.*)$", low)
        if not m or LETTERS.index(m.group(1).upper()) >= len(self.options):
            return None
        self.bundle = self.options[LETTERS.index(m.group(1).upper())].copy()
        self.state = "EDITING"
        rest = m.group(2).strip()
        if rest:
            return self._editing(rest)
        return self._render(self.bundle, "Selected: ") + "\nAny changes? Or say \"confirm\"."

    def _editing(self, text: str) -> str:
        low = text.lower()
        if re.search(DONE, low) or re.match(YES, low):
            return self._confirm()
        new, msg = modify_bundle(self.bundle, text, self.c)
        if new is self.bundle:
            return msg
        problems = [p for p in violations(new, self.c) if "over budget" in p or "contains" in p or "unverified" in p]
        if problems:
            return f"{msg} But that breaks: {'; '.join(problems)}. I've kept the previous order."
        self.bundle = new
        warn = [p for p in violations(new, self.c) if "servings" in p]
        tail = f"\nHeads-up: {'; '.join(warn)}." if warn else ""
        return f"{msg}\n{self._render(new, 'Updated: ')}{tail}\nMore changes, or \"confirm\"?"

    def _confirm(self) -> str:
        try:
            self.cart = self.orders.confirm_cart(self.bundle, self.c, self.now)
        except OrderError as e:
            return f"{e} Let's fix that first."
        self.state = "CONFIRMING"
        b = self.bundle
        return (f"Final check — {b.restaurant.name}\n{self._lines(b)}\n"
                f"  Subtotal {money(b.subtotal)} + GST {money(b.gst)} + delivery & packaging {money(b.restaurant.fees)} "
                f"= {money(b.total)}\n  Arrives ~{b.eta:%H:%M}. Pay with {self.payment} and place the order? (yes / no)")

    def _confirming(self, low: str) -> str:
        methods = self.profile.get("payment_methods") or [self.payment]
        chosen = mentioned_method(low, methods)
        if chosen and chosen != self.payment:
            self.payment = chosen
            if not re.match(YES, low):
                return f"Switched to {chosen}. Place the order for {money(self.bundle.total)}? (yes / no)"
        if re.match(YES, low):
            try:
                order = self.orders.place_order(self.cart.cart_id, self.cart.token, self.idem_key, self.now, self.payment)
            except PaymentDeclined as e:  # design doc: keep the cart, offer another saved method
                others = [m for m in methods if m != e.method]
                if others:
                    self.payment = others[0]
                    return (f"Payment with {e.method} didn't go through. I'm holding your cart until {e.held_until:%H:%M}. "
                            f"Pay with {self.payment} instead? (yes / no)")
                return (f"Payment with {e.method} didn't go through. I'm holding your cart until {e.held_until:%H:%M}; "
                        f"say \"yes\" to try again.")
            except OrderError as e:
                self.state = "EDITING"
                return str(e)
            self.state = "ORDERED"
            note = " I've added your allergy to the restaurant note." if self.c.allergens else ""
            return (f"Order placed — {order['order_id']}, {money(order['total'])}, arriving around {order['eta']}.{note} "
                    f"I'll message you if the ETA slips.")
        self.state = "EDITING"
        if re.match(r"^(no|nope|nah|cancel|wait)\b", low):
            return "No problem — not ordered. What would you like to change?"
        return self._editing(low)

    # ------------------------------------------------------------ rendering
    def _notes(self) -> list[str]:
        notes, self.c.notes = self.c.notes, []
        return notes

    @staticmethod
    def _lines(b: Bundle) -> str:
        rows = []
        for l in b.lines:
            tag = "veg" if l.item.is_veg else ("egg" if l.item.diet == "egg" else "non-veg")
            rows.append(f"  {l.qty}× {l.item.name} ({tag}) — {money(l.item.price * l.qty)}")
        return "\n".join(rows)

    def _render(self, b: Bundle, prefix: str = "") -> str:
        cuisine = ", ".join(x.replace("_", " ").title() for x in b.restaurant.cuisines)
        eta = f" · ETA ~{b.eta:%H:%M}" if b.eta else ""
        head = f"{prefix}{b.restaurant.name} ({cuisine}) — {money(b.total)} incl. GST & fees{eta}"
        out = [head, self._lines(b)]
        from .engine import explain
        reasons = explain(b, self.c)
        if reasons:
            out.append("  Why: " + "; ".join(reasons))
        if b.excluded:
            out.append("  Left out for safety: " + ", ".join(b.excluded[:3]))
        return "\n".join(out)

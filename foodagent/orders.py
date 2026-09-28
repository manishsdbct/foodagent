"""Mock cart, payment and order service. Only place_order has side effects, and it needs a confirm token."""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .engine import violations
from .models import Bundle, Constraints

TOKEN_TTL = timedelta(minutes=5)
PAYMENT_HOLD = timedelta(minutes=10)  # design doc: a failed payment keeps the cart for 10 min


class OrderError(Exception):
    pass


class PaymentDeclined(OrderError):
    def __init__(self, method: str, held_until: datetime):
        super().__init__(f"Payment with {method} was declined. The cart is held until {held_until:%H:%M}.")
        self.method, self.held_until = method, held_until


class MockPaymentGateway:
    """Stand-in for the platform's payment service. Charges are idempotent per key, like a real
    gateway's. Methods listed in `declines` (or FOODAGENT_DECLINE_PAYMENTS, comma-separated) fail."""

    def __init__(self, declines: set[str] | None = None):
        env = os.environ.get("FOODAGENT_DECLINE_PAYMENTS", "")
        self.declines = declines if declines is not None else {m.strip() for m in env.split(",") if m.strip()}
        self.charges: dict[str, str] = {}

    def charge(self, method: str, amount_paise: int, idempotency_key: str) -> str:
        if idempotency_key in self.charges:
            return self.charges[idempotency_key]
        if method in self.declines:
            raise PaymentDeclined(method, datetime.min)  # the order service fills in the hold time
        self.charges[idempotency_key] = f"pay_{secrets.token_hex(6)}"
        return self.charges[idempotency_key]


@dataclass
class Cart:
    cart_id: str
    bundle: Bundle
    token: str
    expires: datetime
    note: str = ""  # restaurant note, auto-filled with the declared allergy

    def price_breakdown(self) -> dict:
        b = self.bundle
        return {"subtotal": b.subtotal, "gst": b.gst, "delivery": b.restaurant.delivery_fee,
                "packaging": b.restaurant.packaging_fee, "total": round(b.total, 2), "total_display": round(b.total)}


class OrderService:
    def __init__(self, log_path: Path | None = None, db_url: str | None = None,
                 payments: MockPaymentGateway | None = None):
        self.carts: dict[str, Cart] = {}
        self.orders_by_key: dict[str, dict] = {}
        self.log_path = log_path
        self.db_url = db_url  # Postgres orders table (db.py); idempotency then survives restarts
        self.payments = payments or MockPaymentGateway()
        self._lock = threading.Lock()  # a retried place_order waits for the first attempt, then sees its order

    def confirm_cart(self, bundle: Bundle, c: Constraints, now: datetime) -> Cart:
        """Re-check every rule server-side, whatever the agent sent, then hold the cart."""
        problems = violations(bundle, c)
        if problems:
            raise OrderError("Cart rejected: " + "; ".join(problems))
        note = ""
        if c.allergens:
            note = ("ALLERGY: " + ", ".join(sorted(a.replace("_", " ") for a in c.allergens))
                    + (" (severe)" if c.severe else "") + ". Please avoid cross-contact.")
        cart = Cart(f"cart_{secrets.token_hex(4)}", bundle, secrets.token_urlsafe(8), now + TOKEN_TTL, note)
        self.carts[cart.cart_id] = cart
        return cart

    def place_order(self, cart_id: str, token: str, idempotency_key: str, now: datetime, payment: str = "saved UPI") -> dict:
        with self._lock:
            return self._place_order(cart_id, token, idempotency_key, now, payment)

    def _place_order(self, cart_id: str, token: str, idempotency_key: str, now: datetime, payment: str) -> dict:
        if idempotency_key in self.orders_by_key:  # retry-safe: never double-charge
            return self.orders_by_key[idempotency_key]
        if self.db_url and (prior := db.find_order(idempotency_key, self.db_url)):
            self.orders_by_key[idempotency_key] = prior
            return prior
        cart = self.carts.get(cart_id)
        if not cart or cart.token != token:
            raise OrderError("Invalid confirmation token.")
        if now > cart.expires:
            raise OrderError("Confirmation expired; please confirm the cart again.")
        b = cart.bundle
        try:  # keyed per method, so switching to another saved method after a decline is a fresh charge
            self.payments.charge(payment, round(b.total * 100), f"{idempotency_key}:{payment}")
        except PaymentDeclined:
            cart.expires = max(cart.expires, now + PAYMENT_HOLD)
            raise PaymentDeclined(payment, cart.expires) from None
        order = {
            "order_id": f"#{b.restaurant.id.split('_')[1][:3].upper()}-{secrets.randbelow(90000) + 10000}",
            "restaurant": b.restaurant.name,
            "items": [{"name": l.item.name, "qty": l.qty, "price": l.item.price} for l in b.lines],
            "subtotal": b.subtotal, "gst": b.gst, "fees": b.restaurant.fees, "total": round(b.total, 2),
            "eta": b.eta.strftime("%H:%M") if b.eta else None,
            "payment": payment, "note": cart.note, "placed_at": now.isoformat(timespec="minutes"),
        }
        if self.db_url:  # before marking it placed, so a failed write can be retried
            db.save_order(order, idempotency_key, b.restaurant.id, [l.item.id for l in b.lines], self.db_url)
        self.orders_by_key[idempotency_key] = order
        del self.carts[cart_id]
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(order) + "\n")
        return order

"""Input and output guardrails around every chat turn, for both the model orchestrator and the
offline rule agent (session.run_turn calls them). Deterministic and offline.

Input, before the agent sees the message:
- oversized messages are refused; control characters are stripped
- payment secrets (card numbers that pass the Luhn check, OTP / CVV / PIN values) are never
  processed: the customer is pointed to saved methods, and the digits are masked in the logs
- prompt-injection attempts ("ignore your instructions", "show your system prompt") and attempts
  to set prices ("make the total ₹0") are refused without reaching the model

Output, before the customer sees the reply:
- API keys, access tokens and live confirm tokens are masked, and so are card numbers
- a reply may say an order was placed only if this session really placed one
- replies are capped in length

The orchestrator also grounds every ₹ amount and dish name in tool results (orchestrator.py), and
the engine and order service enforce allergens, budget and the confirm token whatever the agent says.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_INPUT_CHARS = 1000
MAX_REPLY_CHARS = 4000

CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
PAYMENT_SECRET = re.compile(r"\b(otp|cvv|cvc|upi ?pin|atm ?pin|card ?pin|password)\b\W{0,3}(?:is\W{0,3})?\d{3,8}\b", re.I)
INJECTION = re.compile("|".join([
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules|prompts?|guidelines)\b",
    r"\b(system|developer|hidden)\s+(prompt|message|instructions?)\b",
    r"\b(reveal|show|print|repeat|leak)\b.{0,20}\b(prompt|instructions|api key|secrets?)\b",
    r"\byou are now\b", r"\bjailbreak\b", r"\bdeveloper mode\b", r"\bdo anything now\b",
]), re.I)
PRICE_TAMPER = re.compile(r"\b(make|set|change|put)\b.{0,25}\b(price|prices|total|bill|cost)\b.{0,15}(\b(zero|free)\b|₹\s?0\b|\b0\b)"
                          r"|\bfor free\b|\bfree of cost\b", re.I)
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
ORDER_CLAIM = re.compile(r"\border (?:has been |is |was )?placed\b|\bplaced (?:your|the) order\b", re.I)

REPLY_TOO_LONG = f"That message is too long for me (over {MAX_INPUT_CHARS:,} characters). Could you send a shorter one?"
REPLY_PAYMENT = ("Please don't share card numbers, PINs or OTPs in chat; I haven't used or stored it. "
                 "Orders are paid with your saved payment methods.")
REPLY_INJECTION = ("I can only help with ordering food, and my instructions can't be changed from the chat. "
                   "What would you like to order?")
REPLY_PRICE = ("Prices, taxes and fees come from the restaurant and can't be changed in chat. "
               "I can find options that fit your budget.")
REPLY_NOT_PLACED = "Your order hasn't been placed yet. Confirm the final check and say yes to place it."


@dataclass
class InputCheck:
    text: str                  # what the agent sees (control characters stripped)
    logged: str                # what the logs keep (payment digits masked)
    reply: str | None = None   # set when the message is refused; the agent is then not called
    flags: list[str] = field(default_factory=list)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def mask_cards(text: str) -> tuple[str, bool]:
    found = False

    def sub(m: re.Match) -> str:
        nonlocal found
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found = True
            return "[card number removed]"
        return m.group(0)

    return CARD.sub(sub, text), found


def check_input(message: str) -> InputCheck:
    text = "".join(ch for ch in message if ch.isprintable() or ch in "\n\t").strip()
    logged, card = mask_cards(text)
    logged = PAYMENT_SECRET.sub(lambda m: f"{m.group(1)} [removed]", logged)
    if len(text) > MAX_INPUT_CHARS:
        return InputCheck(text, logged[:MAX_INPUT_CHARS] + "…", REPLY_TOO_LONG, ["input_too_long"])
    if card or PAYMENT_SECRET.search(text):
        return InputCheck(text, logged, REPLY_PAYMENT, ["input_payment_secret"])
    if INJECTION.search(text):
        return InputCheck(text, logged, REPLY_INJECTION, ["input_injection"])
    if PRICE_TAMPER.search(text):
        return InputCheck(text, logged, REPLY_PRICE, ["input_price_tamper"])
    return InputCheck(text, logged)


def _orders(agent):
    return getattr(agent, "orders", None) or getattr(getattr(agent, "session", None), "orders", None)


def _placed_an_order(agent) -> bool:
    if getattr(agent, "state", None) == "ORDERED":
        return True
    trace = getattr(getattr(agent, "session", None), "trace", None) or []
    return any(t.get("tool") == "place_order" and not t.get("error") for t in trace)


def check_output(reply: str, agent) -> tuple[str, list[str]]:
    flags: list[str] = []
    if SECRET.search(reply):
        reply = SECRET.sub("[redacted]", reply)
        flags.append("output_secret_masked")
    orders = _orders(agent)
    for cart in (orders.carts.values() if orders else []):
        if cart.token and cart.token in reply:
            reply = reply.replace(cart.token, "[redacted]")
            flags.append("output_token_masked")
    reply, card = mask_cards(reply)
    if card:
        flags.append("output_card_masked")
    if ORDER_CLAIM.search(reply) and not _placed_an_order(agent):
        reply = REPLY_NOT_PLACED
        flags.append("output_false_order_claim")
    if len(reply) > MAX_REPLY_CHARS:
        reply = reply[:MAX_REPLY_CHARS].rstrip() + "…"
        flags.append("output_truncated")
    return reply, flags

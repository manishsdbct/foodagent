# Demo runbook

About 8 minutes, fully offline. The clock is fixed at 18:45 so ETAs and the 8 PM deadline behave the same every run.

## Before the meeting (5 minutes)

```bash
pg_isready                                        # Postgres must say "accepting connections"
.venv/bin/python -m foodagent.db init             # safe to re-run; prints "40 restaurants, 836 dishes"
.venv/bin/python -m pytest -q                     # expect: 65 passed
.venv/bin/python -m foodagent.web --now 18:45 --no-llm
```

Open http://127.0.0.1:8000 and click **New order** so the page starts clean. Keep a terminal with `python -m foodagent.eval` output ready (it takes about 90 seconds, so run it beforehand).

## The script

| # | Do | Point to |
| --- | --- | --- |
| 1 | Click the **Group dinner** card (six people, two veg, one nut allergy, under ₹2,000, by 8 PM) | "Looking for" tags: the request was understood in one message. "Nut allergy" is read as peanuts and tree nuts, and the reply says so |
| 2 | Scroll the three option cards | Each has a total incl. GST and fees, an ETA before 8 PM, veg/non-veg marks, the reasons it fits, and the dishes **left out for safety** |
| 3 | Click **Choose A** | The order becomes editable |
| 4 | Type `swap garlic naan for missi roti` | Swapped, re-priced and re-checked in one step |
| 5 | Type `swap chicken curry for butter chicken` | **Refused: contains tree nut.** The order is unchanged. Safety is enforced in code, not by the chat model |
| 6 | Press **+** on a dish, then the bin on another | Quantities and removals, each re-checked against budget and allergy |
| 7 | **Looks good — confirm** → **Place order** | Final receipt, then an order number; the allergy is added to the restaurant note |
| 8 | Show the eval output | 240 requests, 0 allergen / diet / budget / deadline violations, release gate PASS |

Optional extras:

- **No-fit case:** New order, then `dinner for 8, all veg, under 1200 by 7:30pm`. It shows the closest options and what to relax, and does not bend the rules.
- **Payment failure:** start the server with `FOODAGENT_DECLINE_PAYMENTS="saved UPI"`. Placing the order says UPI failed, holds the cart for 10 minutes and offers the saved card.

## If something goes wrong

- Page error or stale state: click **New order**, or restart the server (Ctrl+C, same command).
- Database down: `brew services start postgresql@16` (or your Postgres service), then `db init`.
- Browser trouble: the same flow runs in the terminal with `.venv/bin/python -m foodagent.cli --now 18:45 --no-llm`.

## Live Claude mode (optional)

Set `ANTHROPIC_API_KEY` and start the server without `--no-llm`. Claude then plans each turn and writes the replies, while the same engine and checks decide safety and price. In the two dev sessions so far, the first recommendation took about 10 seconds, against 0.3 seconds for the offline agent, and replies may not always render as cards. Use the offline mode for the main demo.

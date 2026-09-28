# Agentic Food-Ordering Assistant — Phase 2 MVP

Takes one request like

> Order dinner for six people. Two are vegetarian, one has a nut allergy. Keep the total under ₹2,000 and deliver it by 8 PM.

and returns three safe, priced meal bundles from different restaurants. You can then edit the one you pick in plain words and place a mock order, from the terminal or a web chat.

Claude plans the turn and writes the replies. It does this by calling six tools. A deterministic engine behind those tools decides what is safe and what everything costs.

## Run

```bash
python -m pip install -r requirements.txt
python -m foodagent.db init                      # one-time: create the Postgres database, schema and catalog
python -m foodagent.cli --now 18:45 --no-llm     # offline rule agent, simulated time 18:45
python -m foodagent.web --now 18:45 --no-llm     # web chat on http://127.0.0.1:8000
export ANTHROPIC_API_KEY=...                     # Claude orchestrator + tools
python -m foodagent.cli --now 18:45
python -m foodagent.eval                         # offline eval suite + release gate (on the database catalog)
python -m foodagent.metrics                      # session metrics from the chat log vs the doc's v1 targets
python -m pytest -q                              # 65 tests (fixed seed catalog + a separate foodagent_test database)
```

Python 3.10+ and a running Postgres 14+. If no API key is set, the CLI and web UI fall back to the offline rule agent, so everything runs without a network.

Environment variables (all optional; see `.env.example`):
- `ANTHROPIC_AGENT_MODEL`: orchestrator model, default `claude-opus-5`.
- `ANTHROPIC_AGENT_EFFORT`: default `low`, because chat is latency-sensitive.
- `ANTHROPIC_MODEL`: model for the rule agent's optional Claude parser, default `claude-haiku-4-5`.
- `DATABASE_URL`: the Postgres database, default `postgresql://localhost/foodagent`.
- `FOODAGENT_DECLINE_PAYMENTS`: comma-separated saved payment methods the mock gateway declines, e.g. `"saved UPI"`, to try the payment-failure flow.
- `TEST_DATABASE_URL`: the database the tests reset and use, default `postgresql://localhost/foodagent_test`.

For a presentation, follow [DEMO.md](DEMO.md).

## Database

All data is read from Postgres at runtime; the files in `foodagent/data/` are only the seed. The schema, in [`foodagent/db_schema.sql`](foodagent/db_schema.sql), follows the design doc's "Database design": money in paise, enums for course, diet, allergen level and taste.

```bash
python -m foodagent.db init                      # create the database if missing, apply the schema, seed an empty catalog
python -m foodagent.db init --reset              # wipe and reload the catalog from the seed files (orders and chat log kept)
python -m foodagent.data.generate_catalog        # regenerate catalog_extra.json (then init --reset)
psql -d foodagent -c "select * from menu"        # or browse in DBeaver
```

Re-running `init` never touches existing data; only `--reset` reloads the catalog. Edits you make in the database (price, `in_stock`, `is_active`) apply to new chat sessions.

| Table | Contents |
| --- | --- |
| `restaurant`, `menu_category` | Outlets (fees, hours, kitchen flags) and their menu sections |
| `dish`, `dish_variant` | A dish's stable facts (course, diet, spice, rating, allergen verification) and its sellable sizes (price, serves, stock). Each variant is one item to the engine |
| `dish_allergen` | What the engine filters on: `declared` rows from the restaurant, plus `derived` rows rebuilt by trigger from `dish_ingredient` → `ingredient_allergen`. A dish is never cleared by one source alone |
| `ingredient`, `ingredient_allergen`, `dish_ingredient` | Recipes; `is_primary` marks the hero ingredient ("paneer dishes") |
| `dish_taste`, `dish_tag`, `canonical_dish`, `canonical_dish_region` | Taste scores, tags, and "famous in Delhi" |
| `dish_variant_price_log` | Append-only price history (trigger) |
| `user_profile`, `user_address` | The customer's profile, addresses and saved payment methods |
| `orders`, `order_item` | Placed orders, unique on the idempotency key (a retried `place_order` is safe even after a restart); they keep their names and prices if the catalog is reloaded |
| `chat_requests` | One row per chat turn (web and CLI): session, message, reply, state, tool calls, error, latency |
| views `menu`, `chat_session` | A flat menu for browsing; one row per chat session for the metrics report |

The JSONL files (`orders.jsonl`, `requests.jsonl`, `trace.jsonl`) are still written as a local copy.

## Try these

| You type | What happens |
| --- | --- |
| the dinner-for-six request above | 3 bundles; cashew gravies and peanut sides left out; a restaurant that would arrive late is skipped |
| `A, but swap garlic naan for missi roti` | picks A, swaps, re-prices, re-checks every rule (on the database catalog) |
| `A, but swap chicken curry for butter chicken` | refused: Butter Chicken contains tree nut; the order is unchanged |
| − / + and the bin on the order card (web) | changes a quantity or removes a dish; each click is an edit the engine re-checks |
| `add butter chicken` | refused: contains tree nut |
| `confirm` → `yes` | final breakdown, then a mock order saved with the allergy in the restaurant note |
| the same, with `FOODAGENT_DECLINE_PAYMENTS="saved UPI"` | payment declined → cart held 10 min → "Pay with card ending 4242 instead?" → `yes` places it |
| `dinner for 4, severe nut allergy` | also drops shared-fryer kitchens (Wok Street, Dragon Wok, …) |
| `suggest me spiccy paneer option` | dish search: paneer as the main ingredient, spice ≥ 3 |
| `dinner for 8, all veg, under 1200 by 7:30pm` | nothing fits → closest options and what to relax |

## Layout

| File | Role (design-doc section) |
| --- | --- |
| `foodagent/data/restaurants.json` | Hand-curated seed: the doc's 6 restaurants, 48 dishes and the user profile, with its traps (cashew gravies, peanut salan, an egg cake at a pure-veg outlet, a shared-fryer kitchen, unverified allergen data). Also the fixed catalog the tests use |
| `foodagent/data/generate_catalog.py`, `catalog_extra.json` | 34 more restaurants, 788 dishes across 9 cuisines (the doc's 30–50 × 20–40), with traps: low-rated, far and closed-at-dinner outlets, out-of-stock dishes, and dishes whose restaurant under-declared an allergen |
| `foodagent/data/ingredient_allergens.json` | Which allergens each ingredient carries (source of the `derived` allergen rows) |
| `foodagent/parser.py` | Free text → constraints. Claude output is validated with Pydantic and retried once. The rule parser is the fallback, and allergens it finds are always added in (Agent loop, step 1) |
| `foodagent/schema.py` | `OrderConstraints`, the doc's tool-facing JSON (groups, budget_inr, soft…), as Pydantic, converted to and from the engine's constraints |
| `foodagent/eta.py` | ETA service: prep + travel, slower at peak, eta_min/eta_max band, deadline buffer |
| `foodagent/bundler.py` | Stage 4 DP: multiple-choice knapsack over (coverage, cost) with Pareto fronts. Keeps the best 2 bundles per restaurant |
| `foodagent/engine.py` | Stages 1–3 and 5: candidates, hard filters, item scoring, ranking and diversity, reason codes. Also edits, substitutes, and the greedy fallback |
| `foodagent/tools.py` | The six tools: `get_user_context`, `recommend_bundles`, `modify_bundle`, `check_eta`, `confirm_cart`, `place_order`. Each call is traced |
| `foodagent/orchestrator.py` | Claude tool loop: max 8 tool calls a turn, prompt caching, `fallbacks: "default"`, and a post-check on every ₹ amount |
| `foodagent/orders.py` | Server-side re-check, 5-minute confirm token, idempotent `place_order`, allergy note, mock payment gateway (a decline holds the cart 10 min) |
| `foodagent/agent.py` | Offline rule agent (state machine), used when no API key is set |
| `foodagent/session.py`, `web.py`, `cli.py` | Session memory with a 2-hour idle TTL, the per-turn runner and log, the web chat (standard library only), the terminal chat |
| `foodagent/db.py`, `db_schema.sql` | Postgres storage and schema: seeding, catalog loader, orders, chat log |
| `foodagent/metrics.py` | Session metrics (conversion, turns to order, latency to first recommendation) against the doc's v1 targets |
| `foodagent/eval.py` | 240 generated requests with labels, an independent auditor, and the release gate |

## Rules the code enforces

- Shared orders are fully free of every declared allergen. `contains` and `may_contain` both disqualify a dish. Dishes with unverified allergen data are excluded for allergic diners. Severe nut allergies also exclude kitchens flagged for shared fryers or cross-contact.
- The LLM cannot drop an allergy. Every allergen the rule parser finds in any customer message is merged into what the tools use, and `confirm_cart` re-checks the cart against the merged set.
- Budget is checked on the payable total: subtotal + 5% GST + delivery + packaging.
- Deadline: ETA + buffer must be no later than the deadline. The buffer is 20 minutes during 19:00–21:30 and 10 minutes otherwise.
- Bundles:
  - enough main and rice/bread servings for everyone, with at most 2 extra of each;
  - at least 2 distinct veg mains for the vegetarians, and a non-veg main for everyone else, when the menu has them;
  - a dal or side when the menu has one;
  - at most 40% of the spend on starters and desserts.
- `place_order` needs the confirm token plus a clean "yes" in a later turn than the one that showed the summary. The session sets the idempotency key, so a retry can't charge twice.
- Every ₹ amount in Claude's reply must appear in a tool result. If one doesn't, the reply goes back for one rewrite. If it still fails, it is replaced with a summary built from the tool data.
- Failures (design doc, "Failure handling"): a declined payment keeps the cart for 10 minutes and offers another saved method; only saved methods are accepted. A tool that fails unexpectedly (database, network) is retried once, then the customer is told plainly and the session keeps its state; the Claude API call gets one retry too. The order row is written before the order counts as placed, and charges are idempotent, so these retries can't double-charge.

## Eval (`python -m foodagent.eval`)

Current result on the 40-restaurant database catalog: 240 requests, 617 bundles shown, 0 allergen, diet, budget or deadline violations, 2633/2633 unsafe add-requests refused, 48/48 sampled conversations ordered cleanly, parse accuracy 100%, p95 recommend 1.3 s (19 ms on the 6-restaurant seed). The run takes about 2 minutes.

## Metrics (`python -m foodagent.metrics`)

Reads the `chat_session` view and scores the doc's session metrics: chat-to-order conversion (target ≥ 30%), median turns to order (≤ 4) and p95 latency to the first recommendation (≤ 4 s), plus the turn error rate. `--since YYYY-MM-DD` and `--agent Orchestrator|Agent` narrow it. On-time delivery needs delivered timestamps from a real logistics API, so it waits for Phase 3.

The auditor recomputes totals, arrival times and allergen hits from the raw data. It checks against the labelled constraints, so a parser miss counts as a violation. One caveat: the requests come from templates. Parse accuracy on real, messier phrasing will be lower, so add a labelled set of real requests before relying on that number.

## Not in this phase

pgvector/Redis (chat sessions stay in memory), add-ons and dish availability windows (tables not created until the engine uses them), semantic search, real ETA and payment APIs, plated-style per-person allergy scoping (plated orders are treated as shared, which is stricter), learning-to-rank, and multi-restaurant orders. See Phases 3–4 in the design doc.

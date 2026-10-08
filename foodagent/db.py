"""Postgres storage: the restaurant catalog, placed orders and the per-turn chat log.

Postgres is the only runtime data source; the files in data/ are the seed. The database is
DATABASE_URL, default postgresql://localhost/foodagent, and the schema is db_schema.sql.

    python -m foodagent.db init          # create the database if needed, apply the schema, seed an empty catalog
    python -m foodagent.db init --reset  # wipe and reload the catalog from the seed files (orders and logs are kept)
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from .models import ALLERGENS, DATA_FILE, EXTRA_FILE, INGREDIENT_ALLERGENS_FILE, Item, Restaurant, read_json

SCHEMA_FILE = Path(__file__).parent / "db_schema.sql"
DEFAULT_URL = "postgresql://localhost/foodagent"
CONNECT_TIMEOUT_S = 5

CATEGORY = {  # course -> (menu section, sort order)
    "starter": ("Starters", 1), "main": ("Mains", 2), "meal": ("Meals", 3), "rice": ("Rice", 4),
    "bread": ("Breads", 5), "noodles": ("Noodles", 6), "side": ("Sides", 7), "dessert": ("Desserts", 8),
    "beverage": ("Beverages", 9),
}


class DatabaseError(RuntimeError):
    pass


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def connect(url: str | None = None):
    import psycopg
    try:
        return psycopg.connect(url or database_url(), autocommit=True, connect_timeout=CONNECT_TIMEOUT_S)
    except psycopg.OperationalError as exc:
        raise DatabaseError(f"Cannot reach Postgres at {url or database_url()}: {_first_line(exc)}"
                            "\nStart Postgres and run: python -m foodagent.db init") from None


def create_database(url: str | None = None) -> bool:
    """CREATE DATABASE for the URL's db name if it does not exist yet. Returns True if created."""
    import psycopg
    from psycopg import sql
    info = psycopg.conninfo.conninfo_to_dict(url or database_url())
    name = info.get("dbname") or "foodagent"
    try:
        conn = psycopg.connect(**{**info, "dbname": "postgres"}, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S)
    except psycopg.OperationalError as exc:
        raise DatabaseError(f"Cannot reach Postgres server for {url or database_url()}: {_first_line(exc)}") from None
    with conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone():
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return True


def paise(rupees: float) -> int:
    return round(rupees * 100)


def rupees(p: int) -> int | float:
    return p // 100 if p % 100 == 0 else p / 100


# ---------------------------------------------------------------- seeding
def seed_files() -> list[Path]:
    return [DATA_FILE] + ([EXTRA_FILE] if EXTRA_FILE.exists() else [])


def init(url: str | None = None, reset: bool = False, files: list[Path] | None = None) -> tuple[int, int]:
    """Apply db_schema.sql; seed the catalog if it is empty (or always, with reset).
    Returns (restaurants, dishes) in the catalog afterwards."""
    with connect(url) as conn, conn.transaction():
        conn.execute(SCHEMA_FILE.read_text())
        if reset:  # orders keep their rows: restaurant_id / dish_id become NULL, names stay
            conn.execute("DELETE FROM restaurant; DELETE FROM canonical_dish; DELETE FROM ingredient; "
                         "DELETE FROM allergen; DELETE FROM user_profile")
        if not conn.execute("SELECT 1 FROM restaurant LIMIT 1").fetchone():
            _seed(conn, files or seed_files())
        n_r, = conn.execute("SELECT count(*) FROM restaurant").fetchone()
        n_d, = conn.execute("SELECT count(*) FROM dish").fetchone()
    return n_r, n_d


def _seed(conn, files: list[Path]) -> None:
    from psycopg.types.json import Jsonb
    raws = [read_json(f) for f in files]
    restaurants = [r for raw in raws for r in raw["restaurants"]]
    profile = next((raw["user_profile"] for raw in raws if raw.get("user_profile")), None)
    dishes = [(r, pos, d) for r in restaurants for pos, d in enumerate(r["items"])]
    ing_allergens = {k: v for k, v in read_json(INGREDIENT_ALLERGENS_FILE).items() if not k.startswith("_")}

    conn.cursor().executemany("INSERT INTO allergen (code, name) VALUES (%s, %s)",
                              [(a, a.replace("_", " ")) for a in ALLERGENS])
    names = sorted({g for _, _, d in dishes for g in d["ingredients"]} | set(ing_allergens))
    ing = dict(conn.execute("INSERT INTO ingredient (name) SELECT unnest(%s::text[]) RETURNING name, id", [names]).fetchall())
    pairs = [(ing[g], a) for g, codes in ing_allergens.items() for a in codes]
    conn.execute("INSERT INTO ingredient_allergen SELECT * FROM unnest(%s::bigint[], %s::text[])",
                 [[p[0] for p in pairs], [p[1] for p in pairs]])

    canon: dict[str, int] = {}
    for _, _, d in dishes:
        name = d.get("canonical") or d["name"]
        if d["famous_in"] and name not in canon:
            canon[name] = conn.execute("INSERT INTO canonical_dish (name, origin) VALUES (%s, %s) RETURNING id",
                                       [name, d["famous_in"][0].title()]).fetchone()[0]
            conn.cursor().executemany("INSERT INTO canonical_dish_region (canonical_dish_id, region_code) VALUES (%s, %s)",
                                      [(canon[name], region) for region in d["famous_in"]])

    rid: dict[str, int] = {}
    for pos, r in enumerate(restaurants):
        rid[r["id"]] = conn.execute(
            "INSERT INTO restaurant (code, position, name, cuisines, rating, pure_veg, prep_time_min, distance_km, "
            "delivery_fee_paise, packaging_fee_paise, kitchen_flags, opens_at, closes_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            [r["id"], pos, r["name"], r["cuisines"], r["rating"], r["pure_veg"], r["prep_time_min"], r["distance_km"],
             paise(r["delivery_fee"]), paise(r["packaging_fee"]), r["kitchen_flags"], r["open"], r["close"]]).fetchone()[0]
        for course in sorted({d["course"] for d in r["items"]}, key=lambda c: CATEGORY[c][1]):
            conn.execute("INSERT INTO menu_category (restaurant_id, name, sort_order) VALUES (%s, %s, %s)",
                         [rid[r["id"]], *CATEGORY[course]])
    cats = {(r, n): i for i, r, n in conn.execute("SELECT id, restaurant_id, name FROM menu_category")}

    rows = defaultdict(list)  # table -> rows, bulk-inserted below (one statement each, so triggers fire once)
    for r, pos, d in dishes:
        dish_id = conn.execute(
            "INSERT INTO dish (code, restaurant_id, category_id, canonical_dish_id, position, name, description, course, "
            "diet_type, spice_level, is_shareable, rating, orders_30d, allergen_verified_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
            [d["id"], rid[r["id"]], cats[(rid[r["id"]], CATEGORY[d["course"]][0])],
             canon.get(d.get("canonical") or d["name"]) if d["famous_in"] else None,
             pos, d["name"], d.get("description"), d["course"], d["diet"], d["spice"], "shareable" in d["tags"],
             d["rating"], d["orders_30d"], d.get("allergen_verified", True)]).fetchone()[0]
        variants = d.get("variants") or [{"label": "Regular", "price": d["price"], "serves": d["serves"],
                                          "in_stock": d.get("in_stock", True)}]
        for n, v in enumerate(variants):
            rows["variant"].append((dish_id, v["label"], paise(v["price"]), v["serves"], n == 0, v.get("in_stock", True)))
        for g in d["ingredients"]:
            rows["ingredient"].append((dish_id, ing[g], g in d["primary"]))
        for level in ("contains", "may_contain"):
            for a in d[level]:
                rows["allergen"].append((dish_id, a, level))
        for taste, intensity in d["tastes"].items():
            rows["taste"].append((dish_id, taste, intensity))
        for tag in d["tags"]:
            if tag != "shareable":
                rows["tag"].append((dish_id, tag))

    def bulk(sql: str, table_rows: list[tuple], types: list[str]) -> None:
        cols = [list(c) for c in zip(*table_rows)] if table_rows else [[] for _ in types]
        conn.execute(sql.format(", ".join(f"%s::{t}[]" for t in types)), cols)

    bulk("INSERT INTO dish_variant (dish_id, label, price_paise, serves, is_default, in_stock) SELECT * FROM unnest({})",
         rows["variant"], ["bigint", "text", "int", "numeric", "bool", "bool"])
    bulk("INSERT INTO dish_ingredient (dish_id, ingredient_id, is_primary) SELECT * FROM unnest({})",
         rows["ingredient"], ["bigint", "bigint", "bool"])
    bulk("INSERT INTO dish_allergen (dish_id, allergen_code, level, source) SELECT d, a, l::allergen_level, 'declared' "
         "FROM unnest({}) AS t(d, a, l)", rows["allergen"], ["bigint", "text", "text"])
    bulk("INSERT INTO dish_taste (dish_id, taste, intensity) SELECT d, t::taste_type, i FROM unnest({}) AS x(d, t, i)",
         rows["taste"], ["bigint", "text", "smallint"])
    bulk("INSERT INTO dish_tag (dish_id, tag) SELECT * FROM unnest({})", rows["tag"], ["bigint", "text"])

    if profile:
        conn.execute("INSERT INTO user_profile (id, name, city, default_address, spice_pref, cuisine_affinity, "
                     "saved_allergies, payment_methods, recent_orders) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     [profile["id"], profile.get("name", ""), profile.get("city"), profile.get("default_address"),
                      profile.get("spice_pref"), Jsonb(profile.get("cuisine_affinity", {})),
                      profile.get("saved_allergies", []), profile.get("payment_methods", []),
                      Jsonb(profile.get("recent_orders", []))])
        conn.cursor().executemany(
            "INSERT INTO user_address (user_id, code, position, label, area) VALUES (%s, %s, %s, %s, %s)",
            [(profile["id"], a["id"], n, a["label"], a["area"]) for n, a in enumerate(profile.get("addresses", []))])


# ---------------------------------------------------------------- catalog
def load_catalog(url: str | None = None) -> tuple[list[Restaurant], dict]:
    """The catalog as the engine uses it: active restaurants (seed order), one Item per sellable
    variant, allergens merged from derived and declared rows, plus the user profile."""
    import psycopg
    with connect(url) as conn:
        try:
            rest = conn.execute(
                "SELECT id, code, name, cuisines, rating, pure_veg, prep_time_min, distance_km, delivery_fee_paise, "
                "packaging_fee_paise, kitchen_flags, to_char(opens_at, 'HH24:MI'), to_char(closes_at, 'HH24:MI') "
                "FROM restaurant WHERE is_active ORDER BY position").fetchall()
        except psycopg.errors.UndefinedTable:
            rest = []
        if not rest:
            raise DatabaseError(f"No restaurants in {url or database_url()}. Run: python -m foodagent.db init")
        dishes = conn.execute(
            "SELECT d.id, d.restaurant_id, d.code, d.name, d.course::text, d.diet_type::text, d.spice_level, d.is_shareable, "
            "d.rating, d.orders_30d, d.allergen_verified_at IS NOT NULL, "
            "coalesce((SELECT array_agg(DISTINCT reg.region_code) FROM canonical_dish_region reg "
            "          WHERE reg.canonical_dish_id = d.canonical_dish_id), '{}') "
            "FROM dish d JOIN restaurant r ON r.id = d.restaurant_id "
            "WHERE d.is_active AND r.is_active ORDER BY r.position, d.position").fetchall()
        variants, allergens, ingredients, tastes, tags = (defaultdict(list) for _ in range(5))
        for dish_id, *v in conn.execute("SELECT dish_id, label, price_paise, serves, in_stock FROM dish_variant "
                                        "ORDER BY dish_id, is_default DESC, id"):
            variants[dish_id].append(v)
        for dish_id, *a in conn.execute("SELECT dish_id, allergen_code, level::text FROM dish_allergen"):
            allergens[dish_id].append(a)
        for dish_id, *g in conn.execute("SELECT di.dish_id, i.name, di.is_primary FROM dish_ingredient di "
                                        "JOIN ingredient i ON i.id = di.ingredient_id"):
            ingredients[dish_id].append(g)
        for dish_id, *t in conn.execute("SELECT dish_id, taste::text, intensity FROM dish_taste"):
            tastes[dish_id].append(t)
        for dish_id, tag in conn.execute("SELECT dish_id, tag FROM dish_tag"):
            tags[dish_id].append(tag)
        profile = _load_profile(conn)

    items: dict[int, list[Item]] = defaultdict(list)
    for dish_id, r_id, code, name, course, diet, spice, shareable, rating, orders, verified, regions in dishes:
        contains = sorted({a for a, level in allergens[dish_id] if level == "contains"})
        may = sorted({a for a, level in allergens[dish_id] if level == "may_contain"} - set(contains))
        vs = variants[dish_id]
        for label, price, serves, in_stock in vs:
            one = len(vs) == 1
            items[r_id].append(Item(
                id=code if one else f"{code}_{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')}",
                name=name if one else f"{name} ({label})", course=course, price=rupees(price), serves=float(serves),
                diet=diet, contains=contains, may_contain=may,
                ingredients=sorted(g for g, _ in ingredients[dish_id]),
                primary=sorted(g for g, p in ingredients[dish_id] if p), spice=spice,
                tastes=dict(sorted(tastes[dish_id])), famous_in=sorted(regions),
                tags=sorted(tags[dish_id] + (["shareable"] if shareable else [])),
                rating=float(rating) if rating is not None else 0.0, orders_30d=orders,
                in_stock=in_stock, allergen_verified=verified))
    restaurants = [Restaurant(id=code, name=name, cuisines=cuisines, rating=float(rating), pure_veg=pure_veg,
                              prep_time_min=prep, distance_km=float(dist), delivery_fee=rupees(dfee),
                              packaging_fee=rupees(pfee), kitchen_flags=flags, open=opens, close=closes,
                              items=items[r_id])
                   for r_id, code, name, cuisines, rating, pure_veg, prep, dist, dfee, pfee, flags, opens, closes in rest]
    return restaurants, profile


def _load_profile(conn) -> dict:
    row = conn.execute("SELECT id, name, city, default_address, spice_pref, cuisine_affinity, saved_allergies, "
                       "payment_methods, recent_orders FROM user_profile ORDER BY id LIMIT 1").fetchone()
    if not row:
        return {}
    keys = ["id", "name", "city", "default_address", "spice_pref", "cuisine_affinity", "saved_allergies",
            "payment_methods", "recent_orders"]
    profile = dict(zip(keys, row))
    profile["addresses"] = [{"id": c, "label": l, "area": a} for c, l, a in conn.execute(
        "SELECT code, label, area FROM user_address WHERE user_id = %s ORDER BY position", [profile["id"]])]
    return profile


# ---------------------------------------------------------------- orders
ORDER_COLS = ("id, restaurant_name, subtotal_paise, gst_paise, fees_paise, total_paise, to_char(eta, 'HH24:MI'), "
              "payment_method, note, placed_at")


def find_order(idempotency_key: str, url: str | None = None) -> dict | None:
    with connect(url) as conn:
        row = conn.execute(f"SELECT {ORDER_COLS} FROM orders WHERE idempotency_key = %s", [idempotency_key]).fetchone()
        if not row:
            return None
        lines = conn.execute("SELECT name, qty, price_paise FROM order_item WHERE order_id = %s ORDER BY line_no",
                             [row[0]]).fetchall()
    oid, restaurant, sub, gst, fees, total, eta, payment, note, placed = row
    return {"order_id": oid, "restaurant": restaurant,
            "items": [{"name": n, "qty": q, "price": rupees(p)} for n, q, p in lines],
            "subtotal": rupees(sub), "gst": rupees(gst), "fees": rupees(fees), "total": rupees(total), "eta": eta,
            "payment": payment, "note": note, "placed_at": placed.isoformat(timespec="minutes")}


def save_order(order: dict, idempotency_key: str, restaurant_code: str, dish_codes: list[str],
               url: str | None = None) -> None:
    with connect(url) as conn, conn.transaction():
        inserted = conn.execute(
            "INSERT INTO orders (id, idempotency_key, restaurant_id, restaurant_name, subtotal_paise, gst_paise, "
            "fees_paise, total_paise, eta, payment_method, note, placed_at) "
            "VALUES (%s, %s, (SELECT id FROM restaurant WHERE code = %s), %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            [order["order_id"], idempotency_key, restaurant_code, order["restaurant"], paise(order["subtotal"]),
             paise(order["gst"]), paise(order["fees"]), paise(order["total"]), order["eta"], order["payment"],
             order["note"], order["placed_at"]]).fetchone()
        if inserted:
            conn.cursor().executemany(
                "INSERT INTO order_item (order_id, line_no, dish_id, name, qty, price_paise) "
                "VALUES (%s, %s, (SELECT id FROM dish WHERE code = %s), %s, %s, %s)",
                [(order["order_id"], n, code, i["name"], i["qty"], paise(i["price"]))
                 for n, (i, code) in enumerate(zip(order["items"], dish_codes), 1)])


# ---------------------------------------------------------------- chat log
def save_request(row: dict, url: str | None = None) -> None:
    from psycopg.types.json import Jsonb
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO chat_requests (request_id, ts, session_id, agent, message, reply, state, tools, error, ms, first_view_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [row["request_id"], row["ts"], row["session_id"], row["agent"], row["message"], row["reply"],
             row["state"], Jsonb(json.loads(json.dumps(row["tools"], default=str))), row["error"], row["ms"], row.get("first_view_ms")])


def main() -> None:
    ap = argparse.ArgumentParser(description="Food assistant Postgres setup")
    ap.add_argument("command", choices=["init"])
    ap.add_argument("--reset", action="store_true", help="wipe and reload the catalog from the seed files")
    args = ap.parse_args()
    try:
        if create_database():
            print(f"Created database {database_url()}")
        n_r, n_d = init(reset=args.reset)
    except DatabaseError as exc:
        raise SystemExit(str(exc))
    print(f"Schema ready in {database_url()}: {n_r} restaurants, {n_d} dishes.")


if __name__ == "__main__":
    main()

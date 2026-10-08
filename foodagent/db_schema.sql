-- Food assistant database schema (Postgres 14+), following the design doc's "Database design".
-- Applied by: python -m foodagent.db init. Every statement is idempotent, so re-running is safe.
--
-- A dish is a stable record; price, serving size and stock live on its variants; allergens are rows
-- the engine can filter with one NOT EXISTS. Money is in paise (integers). Allergen truth is the
-- union of rows derived from the recipe (dish_ingredient -> ingredient_allergen) and rows the
-- restaurant declared; a dish is never cleared by one source alone.
--
-- Not created yet (no code reads them): addon_group/addon_option, dish_availability,
-- ingredient_alias (the parser keeps its own alias list), dish_embedding (Phase 3 search).

-- ---------------------------------------------------------------- types
DO $$ BEGIN CREATE TYPE diet_type AS ENUM ('vegan', 'veg', 'egg', 'non_veg');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE course_type AS ENUM ('starter', 'main', 'bread', 'rice', 'noodles', 'meal', 'side', 'dessert', 'beverage');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE allergen_level AS ENUM ('contains', 'may_contain');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE taste_type AS ENUM ('sweet', 'sour', 'salty', 'bitter', 'rich', 'smoky', 'tangy', 'savoury');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------- legacy (first cut, flat tables)
DROP TABLE IF EXISTS menu_items, restaurants, user_profiles;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'orders' AND column_name = 'subtotal') THEN
    IF EXISTS (SELECT 1 FROM orders) THEN   -- keep old rows under another name
      ALTER TABLE orders RENAME TO orders_v1;
      ALTER INDEX orders_pkey RENAME TO orders_v1_pkey;
      ALTER INDEX orders_idempotency_key_key RENAME TO orders_v1_idempotency_key_key;
    ELSE
      DROP TABLE orders;
    END IF;
  END IF;
END $$;

-- ---------------------------------------------------------------- reference data
CREATE TABLE IF NOT EXISTS allergen (
  code TEXT PRIMARY KEY,                          -- 'peanut', 'tree_nut', 'dairy', ...
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingredient (
  id   BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE                       -- 'cashew', 'paneer', 'gram flour'
);

CREATE TABLE IF NOT EXISTS ingredient_allergen (
  ingredient_id BIGINT NOT NULL REFERENCES ingredient(id) ON DELETE CASCADE,
  allergen_code TEXT   NOT NULL REFERENCES allergen(code),
  PRIMARY KEY (ingredient_id, allergen_code)
);

CREATE TABLE IF NOT EXISTS canonical_dish (
  id     BIGSERIAL PRIMARY KEY,
  name   TEXT NOT NULL UNIQUE,                    -- 'Chole Bhature', 'Butter Chicken'
  origin TEXT
);

CREATE TABLE IF NOT EXISTS canonical_dish_region (
  canonical_dish_id BIGINT NOT NULL REFERENCES canonical_dish(id) ON DELETE CASCADE,
  region_code       TEXT   NOT NULL,              -- 'delhi', 'hyderabad'
  fame_score        NUMERIC(3,2) NOT NULL DEFAULT 1.0 CHECK (fame_score BETWEEN 0 AND 1),
  PRIMARY KEY (canonical_dish_id, region_code)
);

-- ---------------------------------------------------------------- restaurants and menus
CREATE TABLE IF NOT EXISTS restaurant (
  id                  BIGSERIAL PRIMARY KEY,
  code                TEXT NOT NULL UNIQUE,       -- stable public id: 'r_spice_route'
  position            INT  NOT NULL,              -- seed order (stable tie-breaks in ranking)
  name                TEXT NOT NULL,
  cuisines            TEXT[] NOT NULL,
  rating              NUMERIC(2,1) NOT NULL,
  pure_veg            BOOLEAN NOT NULL DEFAULT false,
  prep_time_min       INT NOT NULL CHECK (prep_time_min > 0),
  distance_km         NUMERIC(4,1) NOT NULL,      -- to the customer's area (mock for geo)
  delivery_fee_paise  INT NOT NULL CHECK (delivery_fee_paise >= 0),
  packaging_fee_paise INT NOT NULL CHECK (packaging_fee_paise >= 0),
  kitchen_flags       TEXT[] NOT NULL DEFAULT '{}',  -- 'nut_free_kitchen', 'shared_fryer_nuts'
  opens_at            TIME NOT NULL,
  closes_at           TIME NOT NULL,
  is_active           BOOLEAN NOT NULL DEFAULT true,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS menu_category (
  id            BIGSERIAL PRIMARY KEY,
  restaurant_id BIGINT NOT NULL REFERENCES restaurant(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,                    -- 'Starters', 'Mains', 'Breads'
  sort_order    INT  NOT NULL,
  UNIQUE (restaurant_id, name)
);

CREATE TABLE IF NOT EXISTS dish (
  id                   BIGSERIAL PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE,      -- stable public id: 'sr_kp'
  restaurant_id        BIGINT NOT NULL REFERENCES restaurant(id) ON DELETE CASCADE,
  category_id          BIGINT REFERENCES menu_category(id) ON DELETE SET NULL,
  canonical_dish_id    BIGINT REFERENCES canonical_dish(id),
  position             INT  NOT NULL,             -- menu order
  name                 TEXT NOT NULL,
  description          TEXT,
  course               course_type NOT NULL,
  diet_type            diet_type NOT NULL,
  spice_level          SMALLINT NOT NULL CHECK (spice_level BETWEEN 0 AND 4),
  is_shareable         BOOLEAN NOT NULL DEFAULT true,
  rating               NUMERIC(2,1),
  orders_30d           INT NOT NULL DEFAULT 0,
  allergen_verified_at TIMESTAMPTZ,               -- NULL = unverified, excluded for allergic diners
  is_active            BOOLEAN NOT NULL DEFAULT true,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dish_variant (
  id          BIGSERIAL PRIMARY KEY,
  dish_id     BIGINT NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
  label       TEXT NOT NULL,                      -- 'Regular', 'Half', 'Full', '4 pcs'
  price_paise INT  NOT NULL CHECK (price_paise > 0),
  serves      NUMERIC(3,1) NOT NULL CHECK (serves > 0),
  portion_g   INT,
  is_default  BOOLEAN NOT NULL DEFAULT false,
  in_stock    BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (dish_id, label)
);

-- Append-only, so past orders and analytics stay correct when menus change.
CREATE TABLE IF NOT EXISTS dish_variant_price_log (
  id          BIGSERIAL PRIMARY KEY,
  variant_id  BIGINT NOT NULL REFERENCES dish_variant(id) ON DELETE CASCADE,
  price_paise INT NOT NULL,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dish_ingredient (
  dish_id       BIGINT NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
  ingredient_id BIGINT NOT NULL REFERENCES ingredient(id),
  is_primary    BOOLEAN NOT NULL DEFAULT false,   -- the hero ingredient ("paneer dishes")
  is_removable  BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (dish_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS dish_allergen (
  dish_id       BIGINT NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
  allergen_code TEXT   NOT NULL REFERENCES allergen(code),
  level         allergen_level NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('derived', 'declared')),
  PRIMARY KEY (dish_id, allergen_code, source)
);

CREATE TABLE IF NOT EXISTS dish_taste (
  dish_id   BIGINT NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
  taste     taste_type NOT NULL,
  intensity SMALLINT NOT NULL CHECK (intensity BETWEEN 0 AND 4),
  source    TEXT NOT NULL DEFAULT 'restaurant' CHECK (source IN ('restaurant', 'llm_enriched', 'reviewed', 'feedback')),
  PRIMARY KEY (dish_id, taste)
);

CREATE TABLE IF NOT EXISTS dish_tag (
  dish_id BIGINT NOT NULL REFERENCES dish(id) ON DELETE CASCADE,
  tag     TEXT   NOT NULL,                        -- 'curry', 'dal', 'bakery'
  PRIMARY KEY (dish_id, tag)
);

CREATE INDEX IF NOT EXISTS dish_restaurant_active_idx ON dish (restaurant_id, position) WHERE is_active;
CREATE INDEX IF NOT EXISTS dish_allergen_code_idx     ON dish_allergen (allergen_code, dish_id);
CREATE INDEX IF NOT EXISTS dish_variant_dish_idx      ON dish_variant (dish_id) WHERE in_stock;
CREATE INDEX IF NOT EXISTS dish_search_idx            ON dish USING GIN (to_tsvector('simple', name || ' ' || coalesce(description, '')));

-- Derived allergen rows follow the recipe: rebuilt whenever dish_ingredient or ingredient_allergen changes.
CREATE OR REPLACE FUNCTION rebuild_derived_allergens() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM dish_allergen WHERE source = 'derived';
  INSERT INTO dish_allergen (dish_id, allergen_code, level, source)
  SELECT DISTINCT di.dish_id, ia.allergen_code, 'contains'::allergen_level, 'derived'
  FROM dish_ingredient di JOIN ingredient_allergen ia ON ia.ingredient_id = di.ingredient_id;
  RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS dish_ingredient_allergens ON dish_ingredient;
CREATE TRIGGER dish_ingredient_allergens AFTER INSERT OR UPDATE OR DELETE ON dish_ingredient
  FOR EACH STATEMENT EXECUTE FUNCTION rebuild_derived_allergens();
DROP TRIGGER IF EXISTS ingredient_allergen_allergens ON ingredient_allergen;
CREATE TRIGGER ingredient_allergen_allergens AFTER INSERT OR UPDATE OR DELETE ON ingredient_allergen
  FOR EACH STATEMENT EXECUTE FUNCTION rebuild_derived_allergens();

CREATE OR REPLACE FUNCTION log_variant_price() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR NEW.price_paise IS DISTINCT FROM OLD.price_paise THEN
    INSERT INTO dish_variant_price_log (variant_id, price_paise) VALUES (NEW.id, NEW.price_paise);
  END IF;
  RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS dish_variant_price_history ON dish_variant;
CREATE TRIGGER dish_variant_price_history AFTER INSERT OR UPDATE OF price_paise ON dish_variant
  FOR EACH ROW EXECUTE FUNCTION log_variant_price();

-- ---------------------------------------------------------------- customers
CREATE TABLE IF NOT EXISTS user_profile (
  id               TEXT PRIMARY KEY,              -- 'u_123'
  name             TEXT NOT NULL,
  city             TEXT,
  default_address  TEXT,
  spice_pref       SMALLINT CHECK (spice_pref BETWEEN 0 AND 4),
  cuisine_affinity JSONB NOT NULL DEFAULT '{}',   -- {"north_indian": 0.6, ...}
  saved_allergies  TEXT[] NOT NULL DEFAULT '{}',
  payment_methods  TEXT[] NOT NULL DEFAULT '{}',
  recent_orders    JSONB NOT NULL DEFAULT '[]'    -- order history from before this system
);

CREATE TABLE IF NOT EXISTS user_address (
  user_id  TEXT NOT NULL REFERENCES user_profile(id) ON DELETE CASCADE,
  code     TEXT NOT NULL,                         -- 'home', 'work'
  position INT  NOT NULL,
  label    TEXT NOT NULL,
  area     TEXT NOT NULL,
  PRIMARY KEY (user_id, code)
);

-- ---------------------------------------------------------------- orders
-- The unique idempotency key makes a retried place_order safe, even across restarts.
CREATE TABLE IF NOT EXISTS orders (
  id                  TEXT PRIMARY KEY,           -- '#SPI-33225'
  idempotency_key     TEXT NOT NULL UNIQUE,
  restaurant_id       BIGINT REFERENCES restaurant(id) ON DELETE SET NULL,
  restaurant_name     TEXT NOT NULL,              -- as shown at order time
  subtotal_paise      INT NOT NULL,
  gst_paise           INT NOT NULL,
  fees_paise          INT NOT NULL,
  total_paise         INT NOT NULL,
  eta                 TIME,
  payment_method      TEXT NOT NULL,
  note                TEXT NOT NULL DEFAULT '',
  placed_at           TIMESTAMP NOT NULL,         -- the customer's local time
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_item (
  order_id    TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  line_no     INT  NOT NULL,
  dish_id     BIGINT REFERENCES dish(id) ON DELETE SET NULL,
  name        TEXT NOT NULL,
  qty         INT  NOT NULL CHECK (qty > 0),
  price_paise INT  NOT NULL,                      -- unit price at order time
  PRIMARY KEY (order_id, line_no)
);

-- ---------------------------------------------------------------- chat log and metrics
-- One row per chat turn (web and CLI); tools holds that turn's tool calls.
CREATE TABLE IF NOT EXISTS chat_requests (
  request_id TEXT PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL,
  session_id TEXT NOT NULL,
  agent      TEXT NOT NULL,
  message    TEXT NOT NULL,
  reply      TEXT NOT NULL,
  state      TEXT NOT NULL,                       -- conversation state after the turn
  tools      JSONB NOT NULL,
  error      TEXT,
  ms         REAL NOT NULL
);
-- When the turn's first card reached the page (web, Claude orchestrator); NULL when no card was streamed.
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS first_view_ms REAL;
CREATE INDEX IF NOT EXISTS chat_requests_session ON chat_requests (session_id, ts);

-- Per-session funnel for the design doc's v1 metrics (conversion, turns to order, latency).
DROP VIEW IF EXISTS chat_session;
CREATE VIEW chat_session AS
WITH t AS (
  SELECT *, row_number() OVER (PARTITION BY session_id ORDER BY ts, request_id) AS turn
  FROM chat_requests
)
SELECT session_id,
       min(agent)                                                AS agent,
       min(ts)                                                   AS started_at,
       count(*)                                                  AS turns,
       count(*) FILTER (WHERE error IS NOT NULL)                 AS errors,
       min(turn) FILTER (WHERE state = 'RECOMMENDING')           AS first_recommendation_turn,
       -- time until the options were on screen: the streamed cards when there were any, else the full turn
       (array_agg(coalesce(first_view_ms, ms) ORDER BY turn) FILTER (WHERE state = 'RECOMMENDING'))[1] AS first_recommendation_ms,
       min(turn) FILTER (WHERE state = 'ORDERED')                AS order_turn
FROM t
GROUP BY session_id;

-- Flat menu for browsing (DBeaver, psql): one row per sellable variant.
DROP VIEW IF EXISTS menu;
CREATE VIEW menu AS
SELECT r.code AS restaurant, d.code AS dish_code, d.name AS dish, v.label AS variant, d.course, d.diet_type,
       (v.price_paise / 100.0)::numeric(10,2) AS price_inr, v.serves, d.spice_level, v.in_stock,
       d.allergen_verified_at IS NOT NULL AS allergen_verified,
       (SELECT string_agg(DISTINCT da.allergen_code, ', ') FROM dish_allergen da
         WHERE da.dish_id = d.id AND da.level = 'contains') AS contains,
       (SELECT string_agg(DISTINCT da.allergen_code, ', ') FROM dish_allergen da
         WHERE da.dish_id = d.id AND da.level = 'may_contain'
           AND NOT EXISTS (SELECT 1 FROM dish_allergen c WHERE c.dish_id = d.id
                           AND c.allergen_code = da.allergen_code AND c.level = 'contains')) AS may_contain
FROM dish d
JOIN restaurant r   ON r.id = d.restaurant_id
JOIN dish_variant v ON v.dish_id = d.id
WHERE d.is_active AND r.is_active
ORDER BY r.position, d.position, v.is_default DESC, v.label;

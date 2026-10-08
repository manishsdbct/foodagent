"""Free text -> constraint fields.

Uses Claude (tool use, forced JSON) when ANTHROPIC_API_KEY is set, and a rule-based
parser otherwise. Allergens found by the rules are always unioned in as a safety net,
so an LLM miss can never drop an allergy the customer typed.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import ALLERGENS

WORD_NUM = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split())}
NUM = r"(\d{1,2}|" + "|".join(WORD_NUM) + r")"

INGREDIENT_ALIASES = {
    "paneer": "paneer", "cottage cheese": "paneer", "chicken": "chicken", "murgh": "chicken",
    "mutton": "mutton", "gosht": "mutton", "egg": "egg", "anda": "egg", "chickpea": "chickpea",
    "chole": "chickpea", "chana": "chickpea", "potato": "potato", "aloo": "potato",
    "mushroom": "mushroom", "onion": "onion", "garlic": "garlic", "rice": "rice", "dal": "dal",
}
CUISINES = {
    r"north[- ]indian|punjabi": "north_indian", r"south[- ]indian|udupi|dosa": "south_indian",
    r"biryani": "biryani", r"chinese|indo[- ]chinese|hakka": "indo_chinese",
    r"mughlai|tandoori": "mughlai", r"chaat|street food": "street_food", r"italian": "italian",
    r"bengali": "bengali", r"gujarati": "gujarati",
    # not on the catalog: parsed anyway, so the agent says it is unavailable instead of offering something else
    r"japanese|sushi|ramen": "japanese", r"\bthai\b": "thai", r"mexican|tacos?\b|burritos?": "mexican",
    r"korean": "korean", r"continental": "continental",
}
DISHES = {r"\bpizzas?\b": "pizza", r"\bpastas?\b": "pasta", r"\bdosas?\b": "dosa", r"\bmomos?\b": "momos",
          r"\bthalis?\b": "thali", r"\bburgers?\b": "burger", r"\bsandwich(?:es)?\b": "sandwich"}
REGIONS = ["delhi", "punjab", "hyderabad", "kolkata", "mumbai", "karnataka", "bengaluru", "chennai", "lucknow"]
TASTES = {r"\bsweet|dessert|meetha|mithai": ("sweet", 3), r"\btangy|chatpata|khatta": ("tangy", 3),
          r"\bsmoky|tandoori|charred": ("smoky", 3), r"\brich|creamy|buttery": ("rich", 3)}
# Allergen words, matched only in a clause that also says to avoid them (see _allergens).
ALLERGY_PATTERNS = [
    (r"(?<!tree )(?<!tree-)\bnuts?\b(?!\s*free\s+is\s+fine)", {"peanut", "tree_nut"}),  # a generic nut allergy is both
    (r"peanuts?|groundnuts?|moongphali", {"peanut"}),
    (r"cashews?|almonds?|pistachios?|walnuts?|hazelnuts?|pecans?|tree[- ]nuts?|\bkaju\b|\bbadam\b", {"tree_nut"}),
    (r"gluten|\bwheat\b|celiac|coeliac|\bmaida\b", {"gluten"}),
    (r"dairy|lactose|\bmilk\b(?!\s*shake)|\bcheese\b", {"dairy"}),
    (r"\beggs?\b", {"egg"}),
    (r"\bsoy|\bsoya\b|soybeans?", {"soy"}), (r"sesame|\btil\b", {"sesame"}),
    (r"seafood", {"fish", "shellfish"}),
    (r"shellfish|prawns?|shrimps?|crabs?|lobsters?", {"shellfish"}), (r"(?<!shell)\bfish(?:es)?\b", {"fish"}),
    (r"mustard|\bsarson\b", {"mustard"}),
]
# Words that turn a mention into an avoidance: "allergic to fish", "no milk", "can't have dairy", "gluten-free".
AVOID = (r"allerg|intoleran|\bfree\b|\bno\b|\bnot\b|without|avoid|sensitiv|react|celiac|coeliac|"
         r"(?:can'?t|cannot|can not|don'?t|doesn'?t|do not|does not|won'?t|shouldn'?t|must not|never) (?:eat|have|take|tolerate)")


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else WORD_NUM[tok]


def _parse_time(hour: int, minute: int, ampm: str | None, now: datetime) -> datetime:
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif ampm is None and 1 <= hour < 12 and hour <= now.hour:
        hour += 12  # "by 8" said at 18:45 means 20:00; "by 11" said at 10:00 stays 11:00
    return now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)


def _allergens(t: str) -> set[str]:
    """Allergens named in a clause that also says to avoid them, so "allergic to fish" counts and "we love fish
    curry" does not. Clauses split at sentence ends and at commas; a comma clause made only of allergen words
    continues the list before it ("allergic to fish, milk and eggs")."""
    found: set[str] = set()
    for sentence in re.split(r"[.;!?\n]|\bbut\b", t):
        avoiding = False
        for clause in sentence.split(","):
            hits = set().union(*[codes for pattern, codes in ALLERGY_PATTERNS if re.search(pattern, clause)])
            listing = bool(hits) and not re.sub(r"|".join(p for p, _ in ALLERGY_PATTERNS) + r"|\band\b|\bor\b|\s", "", clause)
            if re.search(r"\b(?:nobody|no one|none of us|not allergic)\b", clause):  # "nobody is allergic to fish"
                avoiding = False
                continue
            avoiding = bool(re.search(AVOID, clause)) or (avoiding and listing)
            if avoiding:
                found |= hits
    return found


def parse_rules(text: str, now: datetime) -> dict:
    t = text.lower().replace("₹", " rs ")
    out: dict = {"notes": []}

    # Headcount: "for six people", "6 guests", "party of 4", "we are 5", "for 4", "4 of us".
    # "one of us can't eat cashew" names a diner, not the group size, so "of us" never matches one.
    m = re.search(rf"\b{NUM}\s+(?:people|persons|guests|pax|adults|friends|members)\b", t) or \
        re.search(rf"\b(?:party|group|table) of\s+{NUM}\b", t) or \
        re.search(rf"\bwe(?: are|'re)\s+{NUM}\b", t) or \
        re.search(rf"\bfor\s+{NUM}\b(?!\s*(?:am|pm|:|o'?clock|rs|rupees|mins?|minutes|hours?))", t) or \
        re.search(rf"\b(?!one\b){NUM}\s+of us\b", t)
    if m:
        out["headcount"] = _num(m.group(1))

    # Vegetarians: "two are vegetarian", "2 veg", "all veg"
    if re.search(r"\b(?:all|everyone|pure|only)\s+(?:are\s+|is\s+)?veg|\bveg\s+only\b", t):
        out["all_veg"] = True
    else:
        m = re.search(rf"\b{NUM}\s+(?:of (?:us|them)\s+)?(?:are\s+|is\s+)?(?:strict\s+)?(?:vegetarians?|veg|vegans?|eggetarians?)\b", t)
        if m:
            out["veg_count"] = _num(m.group(1))

    # Allergies (whole-word nut rule first; peanut never matches \bnut)
    allergens = _allergens(t)
    if re.search(r"\bnuts?\b", t) and not re.search(r"peanut|tree[- ]nut|cashew|almond", t):
        out["notes"].append("I've treated \"nut allergy\" as both peanuts and tree nuts (cashew, almond, pistachio).")
    out["allergens"] = allergens
    if allergens and re.search(r"sever|anaphyla|epi-?pen|serious|life[- ]threatening", t):
        out["severe"] = True
    if re.search(r"eggetarian|eggs? (?:is|are) (?:fine|ok|okay)|(?:veg|vegetarians?) (?:who|that) eats? eggs?|eat eggs?", t):
        out["allow_egg"] = True
    if re.search(r"individual (?:plates|portions)|separate (?:plates|portions)|plated|one each", t):
        out["service_style"] = "plated"
        out["notes"].append("I've still kept every dish allergen-free, not just one plate — cross-contact at the table is a real risk.")

    # Budget: "under 2000", "budget 2,500", "rs 1800", "2k"
    m = re.search(r"(?:under|below|within|less than|max(?:imum)?|budget(?:\s+(?:of|is|to))?|up ?to|not more than|upto)\s*(?:rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", t) \
        or re.search(r"\brs\.?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", t)
    if m:
        amount = float(m.group(1).replace(",", ""))
        out["budget_max"] = int(amount * 1000 if m.group(2) else amount)

    # Deadline: "by 8pm", "before 8:30", "in 45 minutes"
    m = re.search(r"\b(?:by|before|latest)\s+(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm)?", t)
    if m:
        out["deliver_by"] = _parse_time(int(m.group(1)), int(m.group(2) or 0), m.group(3), now)
    else:
        m = re.search(r"\b(?:in|within)\s+(\d{1,3})\s*(?:mins?|minutes)\b", t)
        if m:
            out["deliver_by"] = now + timedelta(minutes=int(m.group(1)))
        elif re.search(r"within (?:an|1) hour", t):
            out["deliver_by"] = now + timedelta(hours=1)

    # Spice (typo-tolerant: spicy, spiccy, spicssy, spicey)
    if re.search(r"not (?:too |very )?spi|mild|less spi|no spice|kids", t):
        out["spice_max"] = 1
    elif re.search(r"\bspi[cs]+\w*y\b|\bhot\b|teekha|fiery", t):
        out["spice_min"] = 4 if re.search(r"extra|very|super|really", t) else 3

    # Tastes
    tastes = {}
    for pattern, (name, level) in TASTES.items():
        if re.search(pattern, t):
            tastes[name] = level
    out["tastes"] = tastes

    # Ingredients to include / exclude
    include, exclude = [], []
    for alias, canon in INGREDIENT_ALIASES.items():
        if re.search(rf"\b(?:no|without|avoid|skip)\s+(?:\w+\s+)?{alias}\b", t):
            exclude.append(canon)
        elif (re.search(rf"\b{alias}\b", t) and not re.search(rf"\b{alias}s?\s+allerg", t)
              and canon not in ("rice", "dal", "onion", "garlic")):
            include.append(canon)
    out["include_ingredients"] = sorted(set(include) - set(exclude))
    out["exclude_ingredients"] = sorted(set(exclude))

    out["cuisines"] = [c for p, c in CUISINES.items() if re.search(p, t)]
    out["dishes"] = [d for p, d in DISHES.items() if re.search(p, t)]
    m = re.search(rf"\b(?:famous in|from|popular in)\s+({'|'.join(REGIONS)})\b|\b({'|'.join(REGIONS)})[- ](?:style|famous|special)", t)
    if m:
        out["region"] = m.group(1) or m.group(2)
    m = re.search(r"\b(breakfast|lunch|dinner|snacks?)\b", t)
    if m:
        out["meal"] = m.group(1)
    return out


CLAUDE_TOOL = {
    "name": "set_order_constraints",
    "description": "Record the food-order constraints stated in the customer's message. Include only fields the message states or clearly implies.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headcount": {"type": "integer", "description": "Number of people eating"},
            "veg_count": {"type": "integer", "description": "How many of them are vegetarian"},
            "all_veg": {"type": "boolean"},
            "allergens": {"type": "array", "items": {"type": "string", "enum": ALLERGENS},
                          "description": "Allergens to exclude. A generic 'nut allergy' means BOTH peanut and tree_nut."},
            "budget_max": {"type": "integer", "description": "Maximum total in INR including taxes and fees"},
            "deliver_by": {"type": "string", "description": "Latest delivery time, 24h HH:MM local time"},
            "include_ingredients": {"type": "array", "items": {"type": "string"}, "description": "Main ingredients wanted, lowercase English (paneer, chicken, chickpea, potato)"},
            "exclude_ingredients": {"type": "array", "items": {"type": "string"}},
            "spice_min": {"type": "integer", "minimum": 0, "maximum": 4, "description": "3 for 'spicy', 4 for 'extra spicy'"},
            "spice_max": {"type": "integer", "minimum": 0, "maximum": 4, "description": "1 for 'mild'/'not spicy'"},
            "tastes": {"type": "object", "additionalProperties": {"type": "integer"},
                       "description": "Minimum taste intensity 0-4, keys: sweet, sour, tangy, smoky, rich"},
            "cuisines": {"type": "array", "items": {"type": "string", "enum": ["north_indian", "south_indian", "biryani", "indo_chinese", "mughlai", "street_food"]}},
            "region": {"type": "string", "description": "Region the dishes should be famous in, lowercase (delhi, hyderabad)"},
            "meal": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
            "allow_egg": {"type": "boolean", "description": "Vegetarians eat egg"},
            "severe": {"type": "boolean", "description": "Allergy described as severe / anaphylactic"},
        },
    },
}


class ParsedFields(BaseModel):
    """Validates what the LLM returns; anything malformed is rejected and the call retried once."""
    model_config = ConfigDict(extra="ignore")

    headcount: int | None = Field(None, ge=1, le=100)
    veg_count: int | None = Field(None, ge=0, le=100)
    all_veg: bool | None = None
    allergens: list[Literal[tuple(ALLERGENS)]] = []
    budget_max: int | None = Field(None, gt=0)
    deliver_by: str | None = Field(None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")
    include_ingredients: list[str] = []
    exclude_ingredients: list[str] = []
    spice_min: int | None = Field(None, ge=0, le=4)
    spice_max: int | None = Field(None, ge=0, le=4)
    tastes: dict[str, int] = {}
    cuisines: list[str] = []
    region: str | None = None
    meal: str | None = None
    allow_egg: bool | None = None
    severe: bool | None = None


def parse_with_claude(text: str, now: datetime) -> dict:
    import anthropic  # imported lazily so the prototype runs without the SDK

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": text}]
    for attempt in range(2):  # the design doc allows one retry on a malformed result
        msg = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            max_tokens=500,
            system=(f"You extract food-order constraints for an Indian food-delivery assistant. "
                    f"Current local time is {now:%Y-%m-%d %H:%M} (Asia/Kolkata). Budgets are in INR. "
                    "Hinglish and typos are common. Never guess values that are not stated."),
            tools=[CLAUDE_TOOL],
            tool_choice={"type": "tool", "name": CLAUDE_TOOL["name"]},
            messages=messages,
        )
        block = next(b for b in msg.content if b.type == "tool_use")
        try:
            data = ParsedFields.model_validate(block.input).model_dump(exclude_none=True)
            break
        except ValidationError as exc:
            if attempt:
                raise
            messages += [{"role": "assistant", "content": msg.content},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                                       "content": f"Invalid fields, please fix: {exc.errors(include_url=False)}"}]}]
    if "deliver_by" in data:
        hh, mm = (int(x) for x in data["deliver_by"].split(":"))
        data["deliver_by"] = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    data["allergens"] = set(data.get("allergens", [])) & set(ALLERGENS)
    return data


def parse(text: str, now: datetime, use_llm: bool = True) -> tuple[dict, str]:
    """Return (fields, source). source is 'claude' or 'rules'."""
    rules = parse_rules(text, now)
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            fields = parse_with_claude(text, now)
            fields["allergens"] = set(fields.get("allergens", set())) | rules["allergens"]  # safety net
            fields["notes"] = rules["notes"]
            return fields, "claude"
        except Exception as exc:  # network, auth, schema: fall back rather than fail the chat
            rules["notes"].append(f"(Claude parser unavailable: {type(exc).__name__}; used rules.)")
    return rules, "rules"

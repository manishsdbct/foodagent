"""OrderConstraints: the tool-facing constraint object from the design doc (Agent loop, step 1).

The LLM orchestrator sends this JSON to recommend_bundles; Pydantic rejects anything malformed
before the engine sees it. It converts to and from the engine's flat Constraints.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import ALLERGENS, DISH_WORDS, Constraints, split_food_wishes

Allergen = Literal[tuple(ALLERGENS)]


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1, le=100)
    diet: Literal["veg", "vegan", "egg", "any"] = "any"
    allergens: list[Allergen] = []


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max: int = Field(gt=0)
    includes: list[Literal["tax", "delivery", "packaging"]] = ["tax", "delivery", "packaging"]


class Range(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: int | None = Field(None, ge=0, le=4)
    max: int | None = Field(None, ge=0, le=4)


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    mode: Literal["famous_in", "trending"] = "famous_in"


class Soft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cuisines: list[str] = Field([], description="Cuisines the customer asked for in this conversation (e.g. italian, "
                                "north_indian, indo_chinese). A requirement: only these restaurants are used. Leave empty "
                                "when the customer did not name one; do not fill it from the profile.")
    dishes: list[str] = Field([], description="Dishes the customer asked for by name (e.g. pizza, biryani, dosa). A "
                              "requirement: every bundle must contain one.")
    spice: Range | None = None
    taste: dict[str, Range] = {}
    include_ingredients: list[str] = []
    exclude_ingredients: list[str] = []
    region: Region | None = None


class OrderConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headcount: int | None = Field(None, ge=1, le=100)
    groups: list[Group] = []
    budget_inr: Budget | None = None
    deliver_by: str | None = Field(None, description="ISO datetime or HH:MM local time")
    meal: str | None = None
    service_style: Literal["shared", "plated"] = "shared"
    severe_allergy: bool = False
    soft: Soft = Soft()
    address_id: str | None = None

    @field_validator("deliver_by")
    @classmethod
    def _time(cls, v: str | None) -> str | None:
        if v is not None:
            _parse_deadline(v, datetime(2000, 1, 1))  # raises ValueError if unreadable
        return v

    @model_validator(mode="after")
    def _groups_fit(self) -> "OrderConstraints":
        total = sum(g.count for g in self.groups)
        if self.headcount and total > self.headcount:
            raise ValueError(f"groups add up to {total}, more than headcount {self.headcount}")
        if not self.headcount and total:
            self.headcount = total
        return self

    # ------------------------------------------------------------ conversion
    def to_constraints(self, now: datetime) -> Constraints:
        c = Constraints()
        c.headcount = self.headcount
        veg = sum(g.count for g in self.groups if g.diet in ("veg", "vegan", "egg"))
        c.veg_count = veg
        c.all_veg = bool(self.headcount) and veg >= self.headcount
        c.allow_egg = any(g.diet == "egg" for g in self.groups)
        c.allergens = {a for g in self.groups for a in g.allergens}
        c.severe = self.severe_allergy
        c.budget_max = self.budget_inr.max if self.budget_inr else None
        c.deliver_by = _parse_deadline(self.deliver_by, now) if self.deliver_by else None
        c.meal = self.meal
        c.service_style = self.service_style
        c.address_id = self.address_id
        s = self.soft
        dish_like = [i for i in s.include_ingredients if i.lower() in DISH_WORDS]  # "pizza" is not an ingredient
        c.cuisines, c.dishes = split_food_wishes(s.cuisines + s.dishes + dish_like)
        if s.spice:
            c.spice_min, c.spice_max = s.spice.min, s.spice.max
        c.tastes = {k: v.min for k, v in s.taste.items() if v.min}
        c.include_ingredients = [i.lower() for i in s.include_ingredients if i.lower() not in DISH_WORDS]
        c.exclude_ingredients = [i.lower() for i in s.exclude_ingredients]
        c.region = s.region.code.lower() if s.region else None
        return c

    @classmethod
    def from_constraints(cls, c: Constraints) -> "OrderConstraints":
        groups = []
        veg = c.effective_veg
        allergic = 1 if c.allergens and c.headcount else 0
        if veg:
            groups.append(Group(count=veg, diet="egg" if c.allow_egg else "veg"))
        rest = (c.headcount or 0) - veg
        if allergic:
            if rest:
                groups.append(Group(count=1, allergens=sorted(c.allergens)))
                rest -= 1
            else:  # everyone is veg: put the allergy on the veg group
                groups[0].allergens = sorted(c.allergens)
        if rest > 0:
            groups.append(Group(count=rest))
        return cls(
            headcount=c.headcount, groups=groups,
            budget_inr=Budget(max=c.budget_max) if c.budget_max else None,
            deliver_by=c.deliver_by.strftime("%H:%M") if c.deliver_by else None,
            meal=c.meal, service_style=c.service_style, severe_allergy=c.severe, address_id=c.address_id,
            soft=Soft(cuisines=c.cuisines, dishes=c.dishes,
                      spice=Range(min=c.spice_min, max=c.spice_max) if c.spice_min or c.spice_max is not None else None,
                      taste={k: Range(min=v) for k, v in c.tastes.items()},
                      include_ingredients=c.include_ingredients, exclude_ingredients=c.exclude_ingredients,
                      region=Region(code=c.region) if c.region else None),
        )


def _parse_deadline(v: str, now: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(v)
        return dt.replace(tzinfo=None)
    except ValueError:
        hh, mm = (int(x) for x in v.split(":"))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)

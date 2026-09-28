"""Delivery ETA service (mock formula for v1; the real logistics API replaces it in Phase 3).

ETA = now + prep + travel. At peak (19:00-21:30) kitchens run slower and roads are busier, so
prep gains 5 min and travel slows from 4 to 5 min/km. The deadline check adds a buffer on top:
10 min off-peak, 20 min at peak, and the order must still arrive by the deadline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .models import Restaurant

PEAK = (time(19, 0), time(21, 30))
PICKUP_MIN = 4          # rider pickup and hand-over
SPREAD_MIN = 5          # eta_min / eta_max band either side of the point estimate


def is_peak(at: datetime) -> bool:
    return PEAK[0] <= at.time() <= PEAK[1]


def buffer_min(at: datetime) -> int:
    return 20 if is_peak(at) else 10


def prep_min(r: Restaurant, now: datetime) -> int:
    return r.prep_time_min + (5 if is_peak(now) else 0)


def travel_min(r: Restaurant, now: datetime) -> int:
    per_km = 5 if is_peak(now) else 4
    return round(r.distance_km * per_km + PICKUP_MIN)


@dataclass
class Eta:
    restaurant_id: str
    eta: datetime             # point estimate shown to the customer
    eta_min: datetime
    eta_max: datetime
    buffer_min: int
    deadline_ok: bool
    margin_min: int | None    # minutes between point ETA and the deadline

    def to_dict(self) -> dict:
        return {"restaurant_id": self.restaurant_id, "eta": f"{self.eta:%H:%M}",
                "eta_min": f"{self.eta_min:%H:%M}", "eta_max": f"{self.eta_max:%H:%M}",
                "buffer_min": self.buffer_min, "deadline_ok": self.deadline_ok,
                "deadline_margin_min": self.margin_min}


def check_eta(r: Restaurant, now: datetime, deliver_by: datetime | None = None) -> Eta:
    arrive = now + timedelta(minutes=prep_min(r, now) + travel_min(r, now))
    buf = buffer_min(arrive)
    spread = timedelta(minutes=SPREAD_MIN + (5 if is_peak(now) else 0))
    ok, margin = True, None
    if deliver_by:
        margin = int((deliver_by - arrive).total_seconds() // 60)
        ok = arrive + timedelta(minutes=buf) <= deliver_by
    return Eta(r.id, arrive, arrive - timedelta(minutes=SPREAD_MIN), arrive + spread, buf, ok, margin)

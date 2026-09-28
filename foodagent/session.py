"""Session memory: one agent per chat, expired after 2 hours idle (Redis with a TTL in production)."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import db
from .orders import OrderService

TTL_S = 2 * 60 * 60
IST = ZoneInfo("Asia/Kolkata")


def local_now(hhmm: str | None = None) -> datetime:
    now = datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)
    if hhmm:
        hh, mm = (int(x) for x in hhmm.split(":"))
        now = now.replace(hour=hh, minute=mm)
    return now


def make_agent(now: datetime, use_llm: bool, orders: OrderService, trace_path: Path | None = None):
    """Claude orchestrator when an API key is set and LLM use is on; otherwise the offline rule agent."""
    if use_llm and os.environ.get("ANTHROPIC_API_KEY"):
        from .orchestrator import Orchestrator
        return Orchestrator(now, orders=orders, trace_path=trace_path)
    from .agent import Agent
    return Agent(now, use_llm=False, orders=orders)


class SessionStore:
    def __init__(self, factory: Callable[[], object], ttl_s: int = TTL_S, clock: Callable[[], float] = time.monotonic):
        self.factory, self.ttl_s, self.clock = factory, ttl_s, clock
        self._items: dict[str, tuple[object, float]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str | None) -> tuple[str, object]:
        with self._lock:
            now = self.clock()
            for sid in [s for s, (_, seen) in self._items.items() if now - seen > self.ttl_s]:
                del self._items[sid]
            if session_id not in self._items:
                session_id = secrets.token_urlsafe(12)
                self._items[session_id] = (self.factory(), now)
            agent, _ = self._items[session_id]
            self._items[session_id] = (agent, now)
            return session_id, agent

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._items.pop(session_id, None)


def run_turn(agent, session_id: str, message: str) -> tuple[str, dict]:
    """One chat turn plus its log row: latency, the tool calls it made, and any error. An exception
    becomes a plain apology and the session is kept (design doc: tell the customer, keep the state)."""
    trace = getattr(getattr(agent, "session", None), "trace", [])  # tool calls (Claude orchestrator only)
    seen, start, error = len(trace), time.perf_counter(), None
    try:
        reply = agent.handle(message)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        reply = (f"Sorry, something went wrong on our side ({type(exc).__name__}). "
                 "Your order details are kept — please try again.")
    row = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "request_id": secrets.token_hex(4),
           "session_id": session_id, "agent": type(agent).__name__, "message": message, "reply": reply,
           "state": agent.state, "tools": trace[seen:], "error": error,
           "ms": round((time.perf_counter() - start) * 1000, 1)}
    return reply, row


class TurnLog:
    """Writes each turn's row to Postgres (chat_requests, which feeds the metrics report), a local
    JSONL file, and optionally a one-line console summary. Logging never breaks the chat."""

    def __init__(self, path: Path | None = None, db_url: str | None = None, echo: bool = True):
        self.path, self.db_url, self.echo = path, db_url, echo
        self._lock = threading.Lock()

    def write(self, row: dict) -> None:
        if self.echo:
            tools = ",".join(t["tool"] for t in row["tools"]) or "-"
            print(f"{row['ts']} {row['request_id']} sid={row['session_id']} {row['state']:<12} {row['ms']:>7.1f}ms "
                  f"tools={tools}{'  ERROR ' + row['error'] if row['error'] else ''}", flush=True)
        if self.path:
            with self._lock, open(self.path, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        if self.db_url:
            try:
                db.save_request(row, self.db_url)
            except Exception as exc:
                print(f"turn log to Postgres failed: {exc}", flush=True)

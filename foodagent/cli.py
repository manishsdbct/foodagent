"""Interactive CLI chat.

    python -m foodagent.cli                 # Claude orchestrator if ANTHROPIC_API_KEY is set, else rule agent
    python -m foodagent.cli --no-llm        # offline rule-based agent
    python -m foodagent.cli --now 18:45     # pretend it is 18:45 today (reproducible demos)
"""
from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from . import db
from .orders import OrderService
from .session import TurnLog, local_now, make_agent, run_turn


def main() -> None:
    ap = argparse.ArgumentParser(description="Agentic food-ordering assistant")
    ap.add_argument("--now", help="simulated local time HH:MM")
    ap.add_argument("--no-llm", action="store_true", help="use the offline rule-based agent")
    args = ap.parse_args()

    now = local_now(args.now)
    llm = not args.no_llm and bool(os.environ.get("ANTHROPIC_API_KEY"))
    db_url = db.database_url()
    orders = OrderService(Path("orders.jsonl"), db_url)
    log = TurnLog(Path("requests.jsonl"), db_url, echo=False)  # every turn feeds the metrics report

    def new_session():
        return secrets.token_urlsafe(12), make_agent(now, llm, orders, Path("trace.jsonl"))

    sid, agent = new_session()
    mode = "Claude orchestrator + tools" if llm else "rule agent (offline)"
    print(f"Food assistant · time {now:%H:%M} · {mode} · type 'quit' to exit, 'restart' for a new order\n")
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"quit", "exit", "bye"}:
            break
        if text.lower() in {"restart", "new order", "start over"}:
            sid, agent = new_session()
            print("\nassistant > Starting fresh. What would you like to order?\n")
            continue
        if text:
            reply, row = run_turn(agent, sid, text)
            log.write(row)
            print("\nassistant > " + reply + "\n")


if __name__ == "__main__":
    main()

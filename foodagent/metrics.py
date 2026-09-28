"""Session metrics from the chat log (design doc, "Evaluation" -> Metrics, the ones measured from sessions).

    python -m foodagent.metrics                        # every logged session
    python -m foodagent.metrics --since 2026-09-28     # sessions started on/after a date
    python -m foodagent.metrics --agent Orchestrator   # only the Claude orchestrator (or Agent)

Reads the chat_session view, which rolls chat_requests up to one row per session. A session
"recommends" when a turn ends in RECOMMENDING and "orders" when a turn ends in ORDERED.
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from . import db

TARGETS = {  # metric -> (target, higher is better)
    "chat_to_order_conversion": (0.30, True),
    "median_turns_to_order": (4, False),
    "p95_first_recommendation_ms": (4000, False),
}


def session_metrics(url: str | None = None, since: date | None = None, agent: str | None = None) -> dict:
    where, params = ["true"], []
    if since:
        where.append("started_at >= %s"); params.append(since)
    if agent:
        where.append("agent = %s"); params.append(agent)
    with db.connect(url) as conn:
        row = conn.execute(f"""
            SELECT count(*),
                   count(*) FILTER (WHERE first_recommendation_turn IS NOT NULL),
                   count(*) FILTER (WHERE order_turn IS NOT NULL),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY order_turn) FILTER (WHERE order_turn IS NOT NULL),
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY first_recommendation_ms)
                       FILTER (WHERE first_recommendation_ms IS NOT NULL),
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY first_recommendation_ms)
                       FILTER (WHERE first_recommendation_ms IS NOT NULL),
                   coalesce(sum(turns), 0)::int, coalesce(sum(errors), 0)::int
            FROM chat_session WHERE {' AND '.join(where)}""", params).fetchone()
    sessions, recommended, ordered, med_turns, p95_ms, med_ms, turns, errors = row
    report = {
        "sessions": sessions,
        "sessions_with_recommendation": recommended,
        "sessions_with_order": ordered,
        "chat_to_order_conversion": round(ordered / recommended, 3) if recommended else None,
        "median_turns_to_order": float(med_turns) if med_turns is not None else None,
        "p95_first_recommendation_ms": round(p95_ms) if p95_ms is not None else None,
        "median_first_recommendation_ms": round(med_ms) if med_ms is not None else None,
        "turns": turns,
        "turn_error_rate": round(errors / turns, 3) if turns else None,
        "on_time_delivery": None,  # needs delivered timestamps from the logistics API (Phase 3)
    }
    report["targets"] = {k: verdict(report[k], *TARGETS[k]) for k in TARGETS}
    return report


def verdict(value, target, higher_is_better: bool) -> str:
    if value is None:
        return "no data"
    ok = value >= target if higher_is_better else value <= target
    return f"{'met' if ok else 'MISSED'} ({'≥' if higher_is_better else '≤'} {target})"


def main() -> None:
    ap = argparse.ArgumentParser(description="Session metrics from the chat log")
    ap.add_argument("--since", type=date.fromisoformat, help="only sessions started on/after YYYY-MM-DD")
    ap.add_argument("--agent", choices=["Orchestrator", "Agent"], help="only one agent type")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args()
    try:
        report = session_metrics(since=args.since, agent=args.agent)
    except db.DatabaseError as exc:
        raise SystemExit(str(exc))
    if args.json:
        print(json.dumps(report, indent=2))
        return
    for k, v in report.items():
        if k == "targets":
            continue
        shown = "n/a (Phase 3)" if k == "on_time_delivery" else ("-" if v is None else v)
        print(f"{k:<32} {shown!s:<10} {report['targets'].get(k, '')}")


if __name__ == "__main__":
    main()

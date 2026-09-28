"""
Monthly voice-call minutes per clinic, for the voice packages in
clinics_store.VOICE_PLANS (Quiet / Average / Busy).

Every phone call is stored by the post-call webhook (app/voice.py) as a
conversation with channel='phone', started_at and ended_at. Billing rules:
  - calls are counted in the calendar month they started in;
  - each call is billed per started minute (61 seconds = 2 minutes);
  - calls shorter than MIN_BILLABLE_SECONDS (hang-ups, wrong numbers) are free.
Reads the clinic's own SQLite file directly, so it never disturbs the
per-request database selection used elsewhere.
"""
import math
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

from shared import clinics_store

MIN_BILLABLE_SECONDS = 10
WARN_AT_FRACTION = 0.8


def _month_bounds(year: int, month: int):
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def previous_month(now: Optional[datetime] = None):
    now = now or datetime.now()
    return (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)


def billable_minutes(clinic: Dict[str, Any], year: int, month: int) -> Dict[str, int]:
    """{'calls': n, 'minutes': billed minutes} for this clinic's phone calls in that month."""
    start, end = _month_bounds(year, month)
    calls = minutes = 0
    try:
        conn = sqlite3.connect(clinic["db_path"])
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT started_at, ended_at FROM conversations "
                "WHERE channel='phone' AND started_at >= ? AND started_at < ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {"calls": 0, "minutes": 0}  # no phone calls stored yet for this clinic
    for r in rows:
        try:
            secs = (datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
        except (TypeError, ValueError):
            continue  # call still being logged / no end time
        if secs < MIN_BILLABLE_SECONDS:
            continue
        calls += 1
        minutes += math.ceil(secs / 60)
    return {"calls": calls, "minutes": minutes}


def usage_summary(clinic: Dict[str, Any], year: int, month: int) -> Dict[str, Any]:
    """Minutes used vs the clinic's package, and what to invoice for that month."""
    plan = clinics_store.voice_plan_of(clinic, year, month)
    used = billable_minutes(clinic, year, month)
    extra = max(0, used["minutes"] - plan["minutes"])
    return {
        "plan": plan,
        "calls": used["calls"],
        "minutes": used["minutes"],
        "included": plan["minutes"],
        "extra_minutes": extra,
        "extra_sar": round(extra * plan["overage_sar"], 2),
        "total_sar": round(plan["price_sar"] + extra * plan["overage_sar"], 2),
        "fraction": used["minutes"] / plan["minutes"] if plan["minutes"] else 0,
    }

"""Pure accounting for estimated hourly energy (all bucket keys are UTC)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta


def timestamp(value: str) -> datetime:
    """Read stored timestamps, including pre-v3 timestamps without an offset."""
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def hour_key(value: datetime) -> str:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def distribute(kwh: float, sessions: list[dict]) -> dict[str, float]:
    """Estimate a constant average power over the supplied active intervals."""
    weights: dict[str, float] = {}
    for session in sessions:
        start, end = timestamp(session["start"]), timestamp(session["end"])
        if end <= start:
            continue
        cursor = timestamp(hour_key(start))
        while cursor < end:
            next_hour = cursor + timedelta(hours=1)
            seconds = (min(end, next_hour) - max(start, cursor)).total_seconds()
            if seconds > 0:
                key = cursor.isoformat()
                weights[key] = weights.get(key, 0.0) + seconds
            cursor = next_hour
    total = sum(weights.values())
    return {key: kwh * seconds / total for key, seconds in weights.items()} if total else {}


def update_day(
    previous: dict | None,
    total_wh: float,
    sessions: list[dict],
    *,
    final: bool,
    fallback: datetime,
) -> dict:
    """Allocate increases to newly completed activity and reconcile revisions.

    A daily total is authoritative only for the amount, never for the hourly
    shape. Keep earlier increments' timing when later cycles report. With no
    new activity, a correction scales the existing shape. Yesterday replaces
    today's total, including downward/zero corrections; it is never added.
    """
    if not math.isfinite(total_wh) or total_wh < 0:
        raise ValueError("Energy must be a finite, non-negative Wh value")
    previous = previous or {}
    if previous.get("final") and not final:
        return previous
    # Only verified yesterday data may revise a day's amount downward.
    if not final and total_wh < previous.get("total_wh", 0):
        return previous
    old = dict(previous.get("hours", {}))
    old_total = sum(old.values())
    total = total_wh / 1000
    cursor = previous.get("assigned_until")
    new_sessions = [
        s for s in sessions if cursor is None or timestamp(s["end"]) > timestamp(cursor)
    ]
    synthetic = previous.get("synthetic", False)
    if (
        total_wh == previous.get("total_wh")
        and final == previous.get("final")
        and not new_sessions
        and not (synthetic and sessions)
    ):
        return previous  # Avoid floating-point drift and repeated recorder writes.
    if old_total == 0:
        new_sessions = sessions

    # A fallback is explicitly an estimate at noon; replace it if real session
    # evidence subsequently becomes available while the date is still mutable.
    if synthetic and sessions:
        old, old_total, new_sessions = {}, 0.0, sessions
        synthetic = False

    if total - old_total > 1e-9 and new_sessions:
        for key, value in distribute(total - old_total, new_sessions).items():
            old[key] = old.get(key, 0.0) + value
        cursor = max(s["end"] for s in sessions)
    elif old_total > 0:
        if total_wh != previous.get("total_wh"):
            old = {key: value * total / old_total for key, value in old.items()}
    elif total > 0 and final:
        old = {hour_key(fallback): total}
        synthetic = True
    # With no known sessions, today's positive total remains pending until a
    # session ends or yesterday's total supplies the explicit noon fallback.
    return {
        "total_wh": total_wh,
        "hours": old,
        "assigned_until": cursor,
        "final": final,
        "synthetic": synthetic,
    }


def combined_hours(ledger: dict) -> dict[str, float]:
    """Sum contributions, including legacy rows sharing an overnight hour."""
    hours = dict(ledger["legacy_hours"])
    for day in ledger["days"].values():
        for key, value in day["hours"].items():
            hours[key] = hours.get(key, 0.0) + value
    # Keep zero rows for removed allocations so old recorder values are erased.
    for key in ledger["written_hours"]:
        hours.setdefault(key, 0.0)
    return hours


def compact(ledger: dict, cutoff: datetime) -> None:
    """Freeze old allocations into the baseline, bounding persistent storage.

    Only entire old reporting days are removed. This preserves overnight
    contributions from reporting days which remain open for reconciliation.
    """
    key = hour_key(cutoff)
    old_days = {
        day
        for day, data in ledger["days"].items()
        if day < cutoff.date().isoformat() and all(hour < key for hour in data["hours"])
    }
    while True:
        retained_hours = [
            hour
            for day, data in ledger["days"].items()
            if day not in old_days
            for hour in data["hours"]
        ]
        if retained_hours:
            key = min(key, min(retained_hours))
        removable = {
            day for day in old_days if all(hour < key for hour in ledger["days"][day]["hours"])
        }
        if removable == old_days:
            break
        old_days = removable
    if key <= ledger["anchor"]:
        return
    hours = combined_hours(ledger)
    # Match recorder's chronological accumulation exactly, including rounding.
    for hour in sorted(hours):
        if hour < key:
            ledger["base_sum"] += hours[hour]
    ledger["legacy_hours"] = {
        hour: value for hour, value in ledger["legacy_hours"].items() if hour >= key
    }
    ledger["days"] = {day: data for day, data in ledger["days"].items() if day not in old_days}
    ledger["written_hours"] = [hour for hour in ledger["written_hours"] if hour >= key]
    ledger["anchor"] = key

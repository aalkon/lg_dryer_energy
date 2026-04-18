"""
Unit tests for lg_dryer_energy attribution logic.

Focus: the bug from the April 2026 live-use incident (duplicate attribution
triggered by energy_yesterday flapping through unknown/unavailable) and its
fix (idempotency by local date, stable baseline, explicit non-numeric state
rejection).

Run: pytest ha-integrations/lg-dryer-energy/tests
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# conftest.py installs HA stubs in sys.modules before this import resolves.
import sys
import os

_PKG_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "custom_components")
)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from lg_dryer_energy import (  # noqa: E402
    DryerSessionTracker,
    STATISTIC_ID,
    _NON_NUMERIC_STATES,
)
import lg_dryer_energy as ldy  # noqa: E402


# ---- helpers ---------------------------------------------------------------


def _make_tracker() -> DryerSessionTracker:
    hass = MagicMock()
    hass.states.get.return_value = None
    tracker = DryerSessionTracker(
        hass,
        status_entity="sensor.dryer_current_status",
        energy_yesterday_entity="sensor.dryer_energy_yesterday",
        active_states=["running", "cooling"],
    )
    # Skip async_start; we set minimal state directly.
    tracker._store._data = None
    return tracker


def _freeze_now(monkeypatch, now_utc: datetime) -> None:
    monkeypatch.setattr(ldy.dt_util, "utcnow", lambda: now_utc)


def _set_stats_rows(reset_stat_state, rows: dict[str, list[dict]]) -> None:
    """Install a stub statistics_during_period on the lg_dryer_energy module.

    Because lg_dryer_energy does `from ... import statistics_during_period`,
    the name is bound at import time and we must patch THAT binding, not the
    source module's attribute.
    """
    reset_stat_state._stats_rows = rows

    def _spd(hass, start_time, end_time, statistic_ids, period, units, types):
        return rows

    ldy.statistics_during_period = _spd


def _event(new_state_val, old_state_val=None):
    new_state = (
        SimpleNamespace(state=new_state_val) if new_state_val is not None else None
    )
    old_state = (
        SimpleNamespace(state=old_state_val) if old_state_val is not None else None
    )
    return ldy.Event({"new_state": new_state, "old_state": old_state})


# ---- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_1_normal_single_run_day(reset_stat_state, local_tz_utc, monkeypatch):
    """Two sessions yesterday → exactly one write summing to 4.0 kWh."""
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    # Yesterday = 2026-04-15 UTC. Two sessions: 07:00-07:30 and 20:00-21:00.
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    t._sessions = [
        {
            "start": (y + timedelta(hours=7)).isoformat(),
            "end": (y + timedelta(hours=7, minutes=30)).isoformat(),
        },
        {
            "start": (y + timedelta(hours=20)).isoformat(),
            "end": (y + timedelta(hours=21)).isoformat(),
        },
    ]
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(4000.0)

    assert len(reset_stat_state._added_calls) == 1
    metadata, stats = reset_stat_state._added_calls[0]
    assert metadata["statistic_id"] == STATISTIC_ID
    # 30-min session = 1/3 of total duration; 60-min = 2/3.
    # Total 4.0 kWh => session1 ~1.333, session2 ~2.667.
    total_delta = sum(s.state for s in stats)
    assert pytest.approx(total_delta, rel=1e-9) == 4.0
    # sums are monotonic
    sums = [s.sum for s in stats]
    assert sums == sorted(sums)
    assert t._last_processed_local_date == "2026-04-15"


@pytest.mark.asyncio
async def test_2_unknown_flap_does_not_retrigger(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """0 → 4000 → unknown → 4000 produces exactly one attribution call."""
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    t._sessions = [
        {
            "start": (y + timedelta(hours=8)).isoformat(),
            "end": (y + timedelta(hours=9)).isoformat(),
        }
    ]
    _set_stats_rows(reset_stat_state, {})

    # Capture tasks scheduled via async_create_task.
    tasks: list = []
    t.hass.async_create_task = lambda coro: tasks.append(coro)

    # Event 1: 0 -> 4000. Should schedule attribution.
    t._async_on_energy_yesterday_change(_event("4000", "0"))
    # Event 2: 4000 -> unknown. Must NOT schedule.
    t._async_on_energy_yesterday_change(_event("unknown", "4000"))
    # Event 3: unknown -> 4000 (flap recovery). Must NOT schedule.
    t._async_on_energy_yesterday_change(_event("4000", "unknown"))

    assert len(tasks) == 1, (
        f"Expected exactly one scheduled attribution; got {len(tasks)}. "
        "Flap recovery must not re-trigger the write path."
    )

    # Run the one legitimate attribution.
    await tasks[0]
    assert len(reset_stat_state._added_calls) == 1


@pytest.mark.asyncio
async def test_3_explicit_replay_safety(reset_stat_state, local_tz_utc, monkeypatch):
    """Calling _async_attribute_energy twice writes once."""
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    t._sessions = [
        {
            "start": (y + timedelta(hours=8)).isoformat(),
            "end": (y + timedelta(hours=9)).isoformat(),
        }
    ]
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(4000.0)
    await t._async_attribute_energy(4000.0)  # replay

    assert len(reset_stat_state._added_calls) == 1


@pytest.mark.asyncio
async def test_4_replay_with_idempotency_bypass_produces_identical_rows(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """
    With the last_processed_local_date guard cleared between runs, the stable
    baseline (fetched from statistics_during_period before yesterday_start)
    must produce byte-identical StatisticData lists.
    """
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    t._sessions = [
        {
            "start": (y + timedelta(hours=7)).isoformat(),
            "end": (y + timedelta(hours=7, minutes=30)).isoformat(),
        },
        {
            "start": (y + timedelta(hours=20)).isoformat(),
            "end": (y + timedelta(hours=21)).isoformat(),
        },
    ]

    # Simulate a pre-existing cumulative sum of 10.0 from two days ago.
    _set_stats_rows(
        reset_stat_state,
        {
            STATISTIC_ID: [
                {
                    "start": (y - timedelta(days=1)).timestamp(),
                    "sum": 10.0,
                }
            ]
        },
    )

    # Save sessions aside, run once, restore, run again with guard bypassed.
    saved_sessions = list(t._sessions)
    await t._async_attribute_energy(4000.0)
    first_stats = list(reset_stat_state._added_calls[-1][1])

    # Bypass: clear the idempotency marker and restore sessions.
    t._last_processed_local_date = None
    t._sessions = saved_sessions
    await t._async_attribute_energy(4000.0)
    second_stats = list(reset_stat_state._added_calls[-1][1])

    assert len(reset_stat_state._added_calls) == 2
    assert len(first_stats) == len(second_stats)
    for a, b in zip(first_stats, second_stats):
        assert a.start == b.start
        assert pytest.approx(a.state, rel=1e-12) == b.state
        assert pytest.approx(a.sum, rel=1e-12) == b.sum


@pytest.mark.asyncio
async def test_5_no_sessions_fallback_writes_once(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """No sessions → noon fallback, single row, flap does not re-fire."""
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    t._sessions = []
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(4000.0)
    assert len(reset_stat_state._added_calls) == 1
    _meta, stats = reset_stat_state._added_calls[0]
    assert len(stats) == 1
    assert stats[0].start.hour == 12  # noon UTC (local == UTC in test fixture)
    assert pytest.approx(stats[0].state, rel=1e-9) == 4.0

    # Simulate flap.
    tasks: list = []
    t.hass.async_create_task = lambda coro: tasks.append(coro)
    t._async_on_energy_yesterday_change(_event("unknown", "4000"))
    t._async_on_energy_yesterday_change(_event("4000", "unknown"))
    assert tasks == []


@pytest.mark.asyncio
async def test_6_session_ending_on_today_is_preserved_not_attributed_to_yesterday(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """
    Session 23:30 yesterday → 00:15 today. Under LG's end-date attribution
    model (v0.1.2), this session's local end-date is TODAY, not yesterday,
    so it does not participate in yesterday's attribution. It is preserved
    unchanged for a future pass once LG reports today's energy. Yesterday
    has no eligible sessions, so the noon-fallback path runs.
    """
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    today = y + timedelta(days=1)
    original = {
        "start": (y + timedelta(hours=23, minutes=30)).isoformat(),
        "end": (today + timedelta(minutes=15)).isoformat(),
    }
    t._sessions = [original]
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(1000.0)  # 1.0 kWh, will go to fallback

    assert len(reset_stat_state._added_calls) == 1
    _meta, stats = reset_stat_state._added_calls[0]
    # Fallback: single row at noon yesterday.
    assert len(stats) == 1
    assert stats[0].start == y + timedelta(hours=12)
    assert pytest.approx(stats[0].state, rel=1e-9) == 1.0

    # Session preserved with ORIGINAL (unclipped) bounds.
    assert t._sessions == [original]


@pytest.mark.asyncio
async def test_8_in_progress_session_not_synthesized_into_yesterday(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """
    Dryer started at 23:55 yesterday, still running when energy_yesterday
    fires. Under the end-date attribution model (v0.1.2), an in-progress
    session has no end time, so it cannot be attributed: LG only reports
    energy after a cycle completes. The session must NOT be synthesized
    into yesterday's attribution; the fallback noon row is used instead.
    _current_session_start must remain untouched so the cycle continues to
    track once it ends.
    """
    now = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    t._sessions = []
    t._current_session_start = datetime(2026, 4, 15, 23, 55, tzinfo=timezone.utc)
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(500.0)

    assert len(reset_stat_state._added_calls) == 1
    _meta, stats = reset_stat_state._added_calls[0]
    # Fallback: single row at noon yesterday, 0.5 kWh.
    assert len(stats) == 1
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    assert stats[0].start == y + timedelta(hours=12)
    assert pytest.approx(stats[0].state, rel=1e-9) == 0.5
    # Critical: live session pointer is untouched.
    assert t._current_session_start == datetime(
        2026, 4, 15, 23, 55, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_10_overnight_cycle_attributed_to_end_date(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """
    Overnight cycle: 23:27:42 local D-1 → 00:19:28 local D. LG attributes
    this cycle's energy to day D (the end-date). When energy_yesterday
    fires on day D+1 reporting 3117 Wh for day D, the integration must:

      - attribute the full 3117 Wh to this single session (whose end-date
        is D), not split/clip it
      - write hourly rows across the session's full hour range, including
        the 23:00 hour on D-1
      - produce approximately 1944 Wh in hour 23:00 D-1 and 1173 Wh in
        hour 00:00 D (proportional to the per-hour overlap of the 3106s
        session)
      - consume the session (move it out of self._sessions) so it is not
        re-attributed later
      - write nothing to hours on D-2 or earlier

    Empirically verified April 2026 from actual CSV data.
    """
    # energy_yesterday fires on day D+1 = 2026-04-19, reporting day D data.
    now = datetime(2026, 4, 19, 6, 16, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    # Session spans midnight between day D-1 (Apr 17) and day D (Apr 18).
    # In the test fixture local_tz == UTC, so local timestamps == UTC.
    session_start = datetime(2026, 4, 17, 23, 27, 42, tzinfo=timezone.utc)
    session_end = datetime(2026, 4, 18, 0, 19, 28, tzinfo=timezone.utc)
    original = {
        "start": session_start.isoformat(),
        "end": session_end.isoformat(),
    }
    t._sessions = [original]
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(3117.0)

    assert len(reset_stat_state._added_calls) == 1
    _meta, stats = reset_stat_state._added_calls[0]

    # Two hourly rows: 23:00 on D-1 (Apr 17) and 00:00 on D (Apr 18).
    starts = [s.start for s in stats]
    assert datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc) in starts
    assert datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc) in starts
    assert len(stats) == 2

    by_hour = {s.start: s.state for s in stats}
    # Duration: 3106 s total. 23:00 hour overlap: 00:00 - 23:27:42 = 1938 s.
    # 00:00 hour overlap: 00:19:28 - 00:00 = 1168 s.
    expected_pre = 3.117 * (1938.0 / 3106.0)   # ~1.944 kWh
    expected_post = 3.117 * (1168.0 / 3106.0)  # ~1.173 kWh
    assert pytest.approx(
        by_hour[datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc)], rel=1e-6
    ) == expected_pre
    assert pytest.approx(
        by_hour[datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc)], rel=1e-6
    ) == expected_post
    # Sum matches reported total.
    assert pytest.approx(sum(s.state for s in stats), rel=1e-9) == 3.117

    # Cumulative sums monotonic.
    sums = [s.sum for s in sorted(stats, key=lambda r: r.start)]
    assert sums == sorted(sums)

    # Nothing written to D-2 (Apr 16) or earlier.
    for s in stats:
        assert s.start >= datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc)

    # Session consumed (not preserved for future re-attribution).
    assert t._sessions == []
    assert t._last_processed_local_date == "2026-04-18"


@pytest.mark.asyncio
async def test_non_monotonic_sum_never_occurs_on_replay(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """
    Regression test for the original bug: the Energy Dashboard showed
    compensating negative bars because a replay wrote a new noon row with
    sum = latest_row.sum + total, making the cumulative non-monotonic.
    After the fix, two replays write identical rows.
    """
    now = datetime(2026, 4, 16, 13, 18, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, now)

    t = _make_tracker()
    y = datetime(2026, 4, 15, tzinfo=timezone.utc)
    t._sessions = [
        {
            "start": (y + timedelta(hours=7)).isoformat(),
            "end": (y + timedelta(hours=7, minutes=30)).isoformat(),
        },
        {
            "start": (y + timedelta(hours=20)).isoformat(),
            "end": (y + timedelta(hours=21)).isoformat(),
        },
    ]
    _set_stats_rows(reset_stat_state, {})

    await t._async_attribute_energy(4200.0)
    first = list(reset_stat_state._added_calls[-1][1])
    last_sum = first[-1].sum

    # Sessions were cleared; simulate the buggy re-trigger scenario by
    # bypassing the date guard and calling again.
    t._last_processed_local_date = None
    await t._async_attribute_energy(4200.0)
    second = list(reset_stat_state._added_calls[-1][1])

    # The baseline fetch returns empty (no stat rows were actually stored
    # in our stub), so second-run sums should equal first-run sums, NOT
    # stack on top of last_sum. Crucially, NO row should have sum > last_sum.
    for row in second:
        assert row.sum <= last_sum + 1e-9, (
            "Non-monotonic cumulative sum detected; replay stacked onto "
            "its own prior output (this is the original bug)."
        )

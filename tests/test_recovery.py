"""Regression coverage for restart-time session reconstruction."""

from __future__ import annotations

import os

# conftest.py installs HA stubs in sys.modules before this import resolves.
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import make_hass

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "custom_components"))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

import lg_dryer_energy as ldy

# ---- helpers ---------------------------------------------------------------


def _freeze_now(monkeypatch, now_utc: datetime) -> None:
    monkeypatch.setattr(ldy.dt_util, "utcnow", lambda: now_utc)


@pytest.mark.asyncio
async def test_11_session_resume_from_history_across_restart(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """
    HA restarted mid-cycle. On startup the dryer is observed as 'running',
    but recorder history shows the run actually began at T1, with an
    earlier non-active state at T0. async_start must walk backward through
    get_last_state_changes and set _current_session_start to T1, NOT to
    utcnow(). This is the dominant cause of truncated session attribution.
    """
    # Freeze utcnow() well AFTER T2 so any fallback to utcnow() would be
    # trivially distinguishable from the expected T1 value.
    T0 = datetime(2026, 4, 19, 14, 0, 0, tzinfo=UTC)  # 'initial'
    T1 = datetime(2026, 4, 19, 14, 12, 0, tzinfo=UTC)  # 'running' (true start)
    T2 = datetime(2026, 4, 19, 14, 25, 0, tzinfo=UTC)  # 'running' (post-restart)
    now_after_restart = datetime(2026, 4, 19, 14, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, now_after_restart)

    # Install a mock get_last_state_changes that returns a contiguous run
    # of active states back to T1 preceded by a non-active state at T0.
    import homeassistant.components.recorder.history as rec_history

    state_entity = "sensor.dryer_current_status"

    def _mk(state_val: str, ts: datetime):
        return SimpleNamespace(state=state_val, last_changed=ts, last_updated=ts)

    history_payload = {
        state_entity: [
            _mk("initial", T0),
            _mk("running", T1),
            _mk("running", T2),
        ]
    }
    calls: list = []

    def _mock_get_last_state_changes(hass, number_of_states, entity_id):
        calls.append((number_of_states, entity_id))
        return history_payload

    monkeypatch.setattr(rec_history, "get_last_state_changes", _mock_get_last_state_changes)

    # Build a hass with states.get returning the currently-running state.
    hass = make_hass()
    hass.states.get.return_value = SimpleNamespace(state="running")

    tracker = ldy.DryerSessionTracker(
        hass,
        status_entity=state_entity,
        energy_yesterday_entity="sensor.dryer_energy_yesterday",
        active_states=["running", "cooling"],
    )

    await tracker.async_start()

    # The reconstruction path must have been used.
    assert calls, "get_last_state_changes was never called on startup"
    assert tracker._current_session_start == T1, (
        f"Expected session start reconstructed to T1={T1.isoformat()}, "
        f"got {tracker._current_session_start}"
    )
    assert tracker._current_session_start != now_after_restart, (
        "Session start fell through to utcnow(); history reconstruction failed."
    )


@pytest.mark.asyncio
async def test_12_session_resume_from_lg_sensors(monkeypatch, local_tz_utc):
    """LG sensors report total=60, remaining=15 -> start reconstructed at now - 45 min."""
    now = datetime(2026, 4, 19, 14, 30, tzinfo=UTC)
    _freeze_now(monkeypatch, now)

    hass = make_hass()

    def _get(eid: str):
        return {
            "sensor.dryer_current_status": SimpleNamespace(state="running"),
            "sensor.dryer_total_time": SimpleNamespace(state="60"),
            "sensor.dryer_remaining_time": SimpleNamespace(state="15"),
        }.get(eid)

    hass.states.get.side_effect = _get

    tracker = ldy.DryerSessionTracker(
        hass,
        status_entity="sensor.dryer_current_status",
        energy_yesterday_entity="sensor.dryer_energy_yesterday",
        active_states=["running", "cooling"],
    )
    tracker.total_time_entity = "sensor.dryer_total_time"
    tracker.remaining_time_entity = "sensor.dryer_remaining_time"

    await tracker.async_start()

    expected = now - timedelta(minutes=45)
    assert tracker._current_session_start == expected


@pytest.mark.asyncio
async def test_13_lg_sensor_resume_skipped_during_cooling(monkeypatch, local_tz_utc):
    """During cooling with remaining=0, tier 1 must return None."""
    now = datetime(2026, 4, 19, 14, 30, tzinfo=UTC)
    _freeze_now(monkeypatch, now)

    hass = make_hass()

    def _get(eid: str):
        return {
            "sensor.dryer_current_status": SimpleNamespace(state="cooling"),
            "sensor.dryer_total_time": SimpleNamespace(state="60"),
            "sensor.dryer_remaining_time": SimpleNamespace(state="0"),
        }.get(eid)

    hass.states.get.side_effect = _get

    tracker = ldy.DryerSessionTracker(
        hass,
        status_entity="sensor.dryer_current_status",
        energy_yesterday_entity="sensor.dryer_energy_yesterday",
        active_states=["running", "cooling"],
    )
    tracker.total_time_entity = "sensor.dryer_total_time"
    tracker.remaining_time_entity = "sensor.dryer_remaining_time"

    result = tracker._resume_from_lg_sensors(now)
    assert result is None


@pytest.mark.parametrize(
    "total,remaining",
    [
        ("unknown", "15"),
        ("60", "unknown"),
        ("not-a-number", "15"),
        ("60", "75"),  # remaining > total
        ("0", "0"),  # total <= 0
        ("-5", "0"),  # negative total
        ("60", "-5"),  # negative remaining
        ("2000", "0"),  # elapsed > 24h
    ],
)
def test_14_lg_sensor_resume_rejects_invalid_values(total, remaining, local_tz_utc):
    now = datetime(2026, 4, 19, 14, 30, tzinfo=UTC)
    hass = make_hass()

    def _get(eid: str):
        return {
            "sensor.dryer_current_status": SimpleNamespace(state="running"),
            "sensor.dryer_total_time": SimpleNamespace(state=total),
            "sensor.dryer_remaining_time": SimpleNamespace(state=remaining),
        }.get(eid)

    hass.states.get.side_effect = _get

    tracker = ldy.DryerSessionTracker(
        hass,
        "sensor.dryer_current_status",
        "sensor.dryer_energy_yesterday",
        ["running", "cooling"],
    )
    tracker.total_time_entity = "sensor.dryer_total_time"
    tracker.remaining_time_entity = "sensor.dryer_remaining_time"

    assert tracker._resume_from_lg_sensors(now) is None


@pytest.mark.asyncio
async def test_15_tier_fallthrough_lg_unavailable_history_succeeds(
    reset_stat_state, local_tz_utc, monkeypatch
):
    """LG sensors unavailable -> tier 1 returns None -> history walk (tier 2) succeeds.

    Guards against regression in tier ordering: if the three-tier resolver
    in async_start is wired incorrectly, the history walk would be skipped
    and _current_session_start would fall through to utcnow().
    """
    T0 = datetime(2026, 4, 19, 14, 0, 0, tzinfo=UTC)
    T1 = datetime(2026, 4, 19, 14, 12, 0, tzinfo=UTC)
    T2 = datetime(2026, 4, 19, 14, 25, 0, tzinfo=UTC)
    now_after_restart = datetime(2026, 4, 19, 14, 30, 0, tzinfo=UTC)
    _freeze_now(monkeypatch, now_after_restart)

    import homeassistant.components.recorder.history as rec_history

    state_entity = "sensor.dryer_current_status"

    def _mk(state_val: str, ts: datetime):
        return SimpleNamespace(state=state_val, last_changed=ts, last_updated=ts)

    history_payload = {
        state_entity: [
            _mk("initial", T0),
            _mk("running", T1),
            _mk("running", T2),
        ]
    }
    calls: list = []

    def _mock_get_last_state_changes(hass, number_of_states, entity_id):
        calls.append((number_of_states, entity_id))
        return history_payload

    monkeypatch.setattr(rec_history, "get_last_state_changes", _mock_get_last_state_changes)

    hass = make_hass()

    def _get(eid: str):
        return {
            "sensor.dryer_current_status": SimpleNamespace(state="running"),
            # LG sensors are unavailable -> tier 1 must return None.
            "sensor.dryer_total_time": SimpleNamespace(state="unavailable"),
            "sensor.dryer_remaining_time": SimpleNamespace(state="unknown"),
        }.get(eid)

    hass.states.get.side_effect = _get

    tracker = ldy.DryerSessionTracker(
        hass,
        status_entity=state_entity,
        energy_yesterday_entity="sensor.dryer_energy_yesterday",
        active_states=["running", "cooling"],
    )
    tracker.total_time_entity = "sensor.dryer_total_time"
    tracker.remaining_time_entity = "sensor.dryer_remaining_time"

    await tracker.async_start()

    assert calls, (
        "get_last_state_changes was never called; tier 2 (history walk) "
        "was not reached when tier 1 (LG sensors) returned None."
    )
    assert tracker._current_session_start == T1, (
        f"Expected fallthrough to history walk producing T1={T1.isoformat()}, "
        f"got {tracker._current_session_start}"
    )
    assert tracker._current_session_start != now_after_restart

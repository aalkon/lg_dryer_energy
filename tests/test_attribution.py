"""Accounting regressions using an in-memory recorder with real upsert semantics."""

import asyncio
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from conftest import add_lg_entity, make_hass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))
import lg_dryer_energy as integration
from lg_dryer_energy.ledger import combined_hours, compact, distribute, update_day
from lg_dryer_energy.source import YesterdaySource

NY = ZoneInfo("America/New_York")


def dt(value):
    return datetime.fromisoformat(value)


def session(start, end):
    return {
        "start": dt(start).astimezone(UTC).isoformat(),
        "end": dt(end).astimezone(UTC).isoformat(),
    }


def state(value, reported, reset=None, unit="Wh"):
    return SimpleNamespace(
        state=str(value),
        last_reported=dt(reported),
        last_changed=dt(reported),
        attributes={"last_reset": reset, "unit_of_measurement": unit},
    )


@pytest.fixture
def env(monkeypatch, reset_stat_state):
    now = [dt("2026-10-08T09:37:00-04:00")]
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: now[0].astimezone(UTC))
    monkeypatch.setattr(integration.dt_util, "_LOCAL_TZ", NY)
    values = {}
    hass = make_hass(running=True)
    hass.states.get.side_effect = values.get
    hass.async_create_task = asyncio.create_task
    tracker = integration.DryerSessionTracker(
        hass, "sensor.dryer_current_status", "sensor.dryer_energy_yesterday", ["running", "cooling"]
    )
    verified = AsyncMock(return_value=None)
    monkeypatch.setattr(tracker._yesterday_source, "async_read", verified)
    tracker._sessions = [session("2026-10-08T08:38:37-04:00", "2026-10-08T08:49:00-04:00")]
    return SimpleNamespace(
        tracker=tracker, values=values, now=now, db=reset_stat_state, hass=hass, verified=verified
    )


def today(env, wh, date="2026-10-08"):
    env.values["sensor.dryer_energy_today"] = state(
        wh, env.now[0].isoformat(), f"{date}T00:00:00-04:00"
    )


def yesterday(env, wh):
    env.values["sensor.dryer_energy_yesterday"] = state(wh, env.now[0].isoformat())
    env.verified.return_value = wh


def sums(env):
    return {dtrow["start"]: dtrow["sum"] for dtrow in env.db._stats_rows[integration.STATISTIC_ID]}


def hours(env):
    return combined_hours(env.tracker._ledger)


@pytest.mark.asyncio
async def test_example_cycle_backdates_to_8am(env):
    today(env, 543)
    await env.tracker._async_refresh()
    assert hours(env) == {"2026-10-08T12:00:00+00:00": pytest.approx(0.543)}
    assert list(sums(env).values())[-1] == pytest.approx(0.543)
    metadata = env.db._added_calls[-1][0]
    assert metadata["mean_type"] == 0 and metadata["unit_class"] == "energy"


@pytest.mark.asyncio
async def test_repeated_reports_and_flaps_are_idempotent(env):
    today(env, 543)
    await env.tracker._async_refresh()
    for value in (543, "unknown", "unavailable", 543):
        today(env, value)
        await env.tracker._async_refresh()
    assert len(env.db._added_calls) == 1


@pytest.mark.asyncio
async def test_next_day_reconciliation_rewrites_later_sums(env):
    today(env, 543)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-09T10:00:00-04:00")
    env.tracker._sessions.append(session("2026-10-09T08:00:00-04:00", "2026-10-09T08:20:00-04:00"))
    today(env, 200, "2026-10-09")
    await env.tracker._async_refresh()
    yesterday(env, 600)
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(0.800)
    assert hours(env)["2026-10-08T12:00:00+00:00"] == pytest.approx(0.6)
    yesterday(env, 500)
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(0.7)
    assert list(sums(env).values()) == sorted(sums(env).values())


@pytest.mark.asyncio
async def test_zero_is_a_valid_final_correction(env):
    today(env, 543)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-09T07:00:00-04:00")
    yesterday(env, 0)
    await env.tracker._async_refresh()
    assert hours(env)["2026-10-08T12:00:00+00:00"] == 0
    assert list(sums(env).values())[-1] == 0


@pytest.mark.asyncio
async def test_second_cycle_gets_only_new_increment(env):
    today(env, 543)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-08T15:00:00-04:00")
    env.tracker._sessions.append(session("2026-10-08T14:00:00-04:00", "2026-10-08T14:30:00-04:00"))
    today(env, 1543)
    await env.tracker._async_refresh()
    assert hours(env) == {
        "2026-10-08T12:00:00+00:00": pytest.approx(0.543),
        "2026-10-08T18:00:00+00:00": pytest.approx(1.0),
    }


@pytest.mark.asyncio
async def test_today_absent_falls_back_to_yesterday(env):
    env.now[0] = dt("2026-10-09T07:00:00-04:00")
    yesterday(env, 543)
    await env.tracker._async_refresh()
    assert sum(hours(env).values()) == pytest.approx(0.543)


@pytest.mark.asyncio
async def test_equal_totals_on_consecutive_days_are_both_counted(env):
    env.now[0] = dt("2026-10-09T07:00:00-04:00")
    yesterday(env, 543)
    await env.tracker._async_refresh()
    env.tracker._sessions.append(session("2026-10-09T14:00:00-04:00", "2026-10-09T14:20:00-04:00"))
    env.now[0] = dt("2026-10-10T07:00:00-04:00")
    yesterday(env, 543)  # Same state, new last_reported: timer observes it.
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(1.086)


@pytest.mark.asyncio
async def test_utc_midnight_does_not_reset_local_reporting_day(env):
    env.now[0] = dt("2026-10-08T21:00:00-04:00")
    env.tracker._sessions = [session("2026-10-08T19:50:00-04:00", "2026-10-08T20:10:00-04:00")]
    today(env, 600)
    await env.tracker._async_refresh()
    assert hours(env) == {
        "2026-10-08T23:00:00+00:00": pytest.approx(0.3),
        "2026-10-09T00:00:00+00:00": pytest.approx(0.3),
    }


@pytest.mark.asyncio
async def test_local_midnight_uses_end_day_and_preserves_shared_hour(env):
    env.now[0] = dt("2026-10-08T23:40:00-04:00")
    env.tracker._sessions = [session("2026-10-08T23:00:00-04:00", "2026-10-08T23:10:00-04:00")]
    today(env, 200)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-09T01:00:00-04:00")
    env.tracker._sessions.append(session("2026-10-08T23:50:00-04:00", "2026-10-09T00:10:00-04:00"))
    today(env, 600, "2026-10-09")
    await env.tracker._async_refresh()
    yesterday(env, 200)
    await env.tracker._async_refresh()
    assert hours(env) == {
        "2026-10-09T03:00:00+00:00": pytest.approx(0.5),
        "2026-10-09T04:00:00+00:00": pytest.approx(0.3),
    }
    assert list(sums(env).values())[-1] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_stale_today_at_midnight_not_treated_as_new_day(env):
    today(env, 543)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-09T00:10:00-04:00")
    await env.tracker._async_refresh()
    assert set(env.tracker._ledger["days"]) == {"2026-10-08"}


@pytest.mark.asyncio
async def test_stale_yesterday_is_ignored(env):
    env.values["sensor.dryer_energy_yesterday"] = state(100, env.now[0].isoformat())
    env.now[0] = dt("2026-10-09T00:10:00-04:00")
    await env.tracker._async_refresh()
    assert not env.db._added_calls


@pytest.mark.asyncio
async def test_running_cycle_is_deferred_until_completion(env):
    env.tracker._sessions = []
    env.tracker._current_session_start = dt("2026-10-08T12:38:37+00:00")
    today(env, 543)
    await env.tracker._async_refresh()
    assert not env.db._added_calls
    await env.tracker._async_on_status_change(
        integration.Event({"new_state": SimpleNamespace(state="end")})
    )
    await env.tracker._refresh_task
    assert sum(hours(env).values()) == pytest.approx(0.543)


@pytest.mark.asyncio
async def test_session_start_persisted_and_unavailable_does_not_end_it(env):
    await env.tracker._async_on_status_change(
        integration.Event({"new_state": SimpleNamespace(state="running")})
    )
    await env.tracker._refresh_task
    start = env.tracker._current_session_start
    assert env.tracker._store._data["current_session_start"] == start.isoformat()
    await env.tracker._async_on_status_change(
        integration.Event({"new_state": SimpleNamespace(state="unavailable")})
    )
    assert env.tracker._current_session_start == start


@pytest.mark.asyncio
async def test_restart_replay_preserves_cumulative_total(env):
    today(env, 543)
    await env.tracker._async_refresh()
    stored = deepcopy(env.tracker._store._data)
    restored = integration.DryerSessionTracker(
        env.hass,
        env.tracker.status_entity,
        env.tracker.energy_yesterday_entity,
        ["running", "cooling"],
    )
    restored._store._data = stored
    await restored.async_start()
    await restored._refresh_task
    assert len(env.db._added_calls) == 1
    assert list(sums(env).values())[-1] == pytest.approx(0.543)
    await restored.async_stop()


@pytest.mark.asyncio
async def test_failed_recorder_enqueue_replays_durable_intent(env, monkeypatch):
    original = integration.async_add_external_statistics
    monkeypatch.setattr(
        integration,
        "async_add_external_statistics",
        MagicMock(side_effect=RuntimeError("DB offline")),
    )
    today(env, 543)
    await env.tracker._async_refresh()
    assert env.tracker._store._data["ledger"]["dirty"]
    monkeypatch.setattr(integration, "async_add_external_statistics", original)
    await env.tracker._async_refresh()
    assert not env.tracker._store._data["ledger"]["dirty"]
    assert list(sums(env).values())[-1] == pytest.approx(0.543)


@pytest.mark.asyncio
async def test_silent_recorder_failure_keeps_dirty_until_verified(env, monkeypatch):
    original = integration.async_add_external_statistics
    monkeypatch.setattr(integration, "async_add_external_statistics", MagicMock())
    today(env, 543)
    await env.tracker._async_refresh()
    assert env.tracker._ledger["dirty"]
    monkeypatch.setattr(integration, "async_add_external_statistics", original)
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(0.543)


@pytest.mark.asyncio
async def test_concurrent_refreshes_do_not_double_count(env):
    today(env, 543)
    await asyncio.gather(*(env.tracker._async_refresh() for _ in range(5)))
    assert len(env.db._added_calls) == 1


@pytest.mark.asyncio
async def test_legacy_migration_keeps_total_after_long_idle_gap(env):
    env.db._stats_rows[integration.STATISTIC_ID] = [
        {"start": dt("2026-08-01T12:00:00+00:00").timestamp(), "sum": 100}
    ]
    env.tracker._last_processed_local_date = "2026-08-01"
    today(env, 543)
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(100.543)


@pytest.mark.asyncio
async def test_missing_storage_with_existing_statistics_fails_closed(env):
    env.db._stats_rows[integration.STATISTIC_ID] = [
        {"start": dt("2026-10-08T12:00:00+00:00").timestamp(), "sum": 10}
    ]
    today(env, 543)
    await env.tracker._async_refresh()
    assert not env.db._added_calls


@pytest.mark.asyncio
async def test_query_failure_does_not_zero_existing_baseline(env, monkeypatch):
    monkeypatch.setattr(
        integration, "statistics_during_period", MagicMock(side_effect=RuntimeError("DB offline"))
    )
    today(env, 543)
    await env.tracker._async_refresh()
    assert env.tracker._ledger is None
    assert not env.db._added_calls


@pytest.mark.parametrize("value", ["unknown", "unavailable", "nan", "inf", "-1", "bad"])
@pytest.mark.asyncio
async def test_invalid_energy_is_ignored(env, value):
    today(env, value)
    await env.tracker._async_refresh()
    assert not env.db._added_calls


@pytest.mark.asyncio
async def test_kwh_unit_is_converted(env):
    today(env, 0.543)
    env.values["sensor.dryer_energy_today"].attributes["unit_of_measurement"] = "kWh"
    await env.tracker._async_refresh()
    assert sum(hours(env).values()) == pytest.approx(0.543)


@pytest.mark.parametrize(
    "start,end,keys",
    [
        (
            "2026-03-08T01:50:00-05:00",
            "2026-03-08T03:10:00-04:00",
            ["2026-03-08T06:00:00+00:00", "2026-03-08T07:00:00+00:00"],
        ),
        (
            "2026-11-01T01:50:00-04:00",
            "2026-11-01T01:10:00-05:00",
            ["2026-11-01T05:00:00+00:00", "2026-11-01T06:00:00+00:00"],
        ),
    ],
)
def test_dst_splits_by_elapsed_utc_time(start, end, keys):
    buckets = distribute(0.6, [session(start, end)])
    assert buckets == {key: pytest.approx(0.3) for key in keys}


@pytest.mark.asyncio
async def test_noon_fallback_is_local_noon_on_dst_day(env):
    env.tracker._sessions = []
    env.now[0] = dt("2026-03-09T07:00:00-04:00")
    yesterday(env, 600)
    await env.tracker._async_refresh()
    assert hours(env) == {"2026-03-08T16:00:00+00:00": pytest.approx(0.6)}


@pytest.mark.asyncio
async def test_migration_preserves_sessions_and_marker():
    store = integration._LgDryerStore(MagicMock(), 3, "test")
    old = {"sessions": [{"start": "example"}], "last_processed_local_date": "2026-10-07"}
    assert await store._async_migrate_func(2, 1, old) == {**old, "ledger": None}


def test_final_total_does_not_get_added_to_provisional():
    sessions = [session("2026-10-08T08:00:00-04:00", "2026-10-08T08:30:00-04:00")]
    provisional = update_day(
        None, 543, sessions, final=False, fallback=dt("2026-10-08T12:00:00-04:00")
    )
    final = update_day(
        provisional, 600, sessions, final=True, fallback=dt("2026-10-08T12:00:00-04:00")
    )
    assert sum(final["hours"].values()) == pytest.approx(0.6)
    assert (
        update_day(final, 700, sessions, final=False, fallback=dt("2026-10-08T12:00:00-04:00"))
        == final
    )


def test_compaction_preserves_total_and_bounds_ledger():
    ledger = {
        "anchor": "2026-09-01T00:00:00+00:00",
        "base_sum": 10.0,
        "legacy_hours": {},
        "days": {},
        "written_hours": [],
    }
    for day in range(1, 31):
        key = f"2026-09-{day:02d}T12:00:00+00:00"
        ledger["days"][f"2026-09-{day:02d}"] = {"hours": {key: 0.5}}
        ledger["written_hours"].append(key)
    compact(ledger, dt("2026-09-20T00:00:00+00:00"))
    assert ledger["base_sum"] + sum(combined_hours(ledger).values()) == pytest.approx(25.0)
    assert len(ledger["days"]) == 11


@pytest.mark.asyncio
async def test_zero_correction_then_positive_restores_session_hours(env):
    today(env, 543)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-09T07:00:00-04:00")
    yesterday(env, 0)
    await env.tracker._async_refresh()
    yesterday(env, 600)
    await env.tracker._async_refresh()
    assert hours(env) == {"2026-10-08T12:00:00+00:00": pytest.approx(0.6)}


@pytest.mark.asyncio
async def test_committed_but_unacknowledged_write_replays_after_restart(env):
    today(env, 543)
    await env.tracker._async_refresh()
    stored = deepcopy(env.tracker._store._data)
    stored["ledger"]["dirty"] = True  # Crash after DB commit, before storage acknowledgement.
    restored = integration.DryerSessionTracker(
        env.hass, env.tracker.status_entity, env.tracker.energy_yesterday_entity, ["running"]
    )
    restored._store._data = stored
    await restored.async_start()
    await restored._refresh_task
    assert not restored._ledger["dirty"]
    assert list(sums(env).values())[-1] == pytest.approx(0.543)
    await restored.async_stop()


@pytest.mark.asyncio
async def test_failed_intent_save_does_not_enqueue_and_retries(env, monkeypatch):
    from unittest.mock import AsyncMock

    original = env.tracker._store.async_save
    monkeypatch.setattr(
        env.tracker._store, "async_save", AsyncMock(side_effect=OSError("Disk full"))
    )
    today(env, 543)
    await env.tracker._async_refresh()
    assert not env.db._added_calls
    assert env.tracker._ledger["days"] == {}
    monkeypatch.setattr(env.tracker._store, "async_save", original)
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(0.543)


@pytest.mark.asyncio
async def test_migration_preserves_legacy_contribution_in_shared_overnight_hour(env):
    shared = "2026-10-08T03:00:00+00:00"
    env.db._stats_rows[integration.STATISTIC_ID] = [
        {"start": dt("2026-10-06T12:00:00+00:00").timestamp(), "sum": 10.0},
        {"start": dt(shared).timestamp(), "sum": 10.2},
    ]
    env.tracker._last_processed_local_date = "2026-10-07"
    env.tracker._sessions = [session("2026-10-07T23:50:00-04:00", "2026-10-08T00:10:00-04:00")]
    today(env, 600)
    await env.tracker._async_refresh()
    assert hours(env)[shared] == pytest.approx(0.5)
    assert list(sums(env).values())[-1] == pytest.approx(10.8)


def test_compaction_preserves_overlapping_reporting_days():
    shared = "2026-09-19T23:00:00+00:00"
    later = "2026-09-20T00:00:00+00:00"
    ledger = {
        "anchor": "2026-09-01T00:00:00+00:00",
        "base_sum": 10.0,
        "legacy_hours": {},
        "days": {
            "2026-09-18": {"hours": {"2026-09-18T12:00:00+00:00": 0.5}},
            "2026-09-19": {"hours": {shared: 0.2}},
            "2026-09-20": {"hours": {shared: 0.3, later: 0.3}},
        },
        "written_hours": [],
    }
    compact(ledger, dt(later))
    assert ledger["anchor"] == shared
    assert ledger["base_sum"] == pytest.approx(10.5)
    assert combined_hours(ledger) == {shared: pytest.approx(0.5), later: pytest.approx(0.3)}
    compact(ledger, dt("2026-09-21T00:00:00+00:00"))
    assert ledger["base_sum"] == pytest.approx(11.3)
    assert not ledger["days"]


@pytest.mark.asyncio
async def test_dirty_restart_defers_all_imports_until_ha_started(env, monkeypatch):
    today(env, 543)
    await env.tracker._async_refresh()
    stored = deepcopy(env.tracker._store._data)
    stored["ledger"]["dirty"] = True
    env.hass.state = integration.CoreState.starting
    restarted = integration.DryerSessionTracker(
        env.hass, env.tracker.status_entity, env.tracker.energy_yesterday_entity, ["running"]
    )
    restarted._store._data = stored
    recorder = integration.get_instance(env.hass)
    original = recorder.async_block_till_done

    async def commit_after_startup():
        assert env.hass.state is integration.CoreState.running
        await original()

    monkeypatch.setattr(recorder, "async_block_till_done", commit_after_startup)
    await asyncio.wait_for(restarted.async_start(), 1)
    restarted._queue_refresh()  # State-change/timer callbacks before started.
    await restarted._async_refresh()  # Defensive guard on the coroutine too.
    assert restarted._refresh_task is None
    assert len(env.db._added_calls) == 1
    assert restarted._ledger["dirty"]
    env.hass.state = integration.CoreState.running
    for handler in tuple(env.hass._started_callbacks):
        handler(env.hass)
    await restarted._refresh_task
    assert not restarted._ledger["dirty"]
    assert list(sums(env).values())[-1] == pytest.approx(0.543)
    await restarted.async_stop()


@pytest.mark.asyncio
async def test_stop_before_start_cancels_deferred_refresh(env):
    env.hass.state = integration.CoreState.starting
    await env.tracker.async_start()
    await env.tracker.async_stop()
    assert not env.hass._started_callbacks
    env.hass.state = integration.CoreState.running
    env.tracker._queue_refresh()
    assert env.tracker._refresh_task is None


@pytest.mark.asyncio
async def test_silent_store_failure_during_migration_cannot_double_count(env, monkeypatch):
    env.db._stats_rows[integration.STATISTIC_ID] = [
        {"start": dt("2026-10-07T12:00:00+00:00").timestamp(), "sum": 100}
    ]
    env.tracker._last_processed_local_date = "2026-10-07"
    await env.tracker._async_save()
    stored = deepcopy(env.tracker._store._data)
    assert env.tracker._store.atomic_writes
    # The file-backed Store catches this error and returns normally, just as HA does.
    monkeypatch.setattr(
        env.tracker._store, "_write_data", MagicMock(side_effect=OSError("Disk full"))
    )
    today(env, 543)
    await env.tracker._async_refresh()
    assert not env.db._added_calls
    assert env.tracker._store._data == stored
    assert env.tracker._ledger["days"] == {}
    restarted = integration.DryerSessionTracker(
        env.hass, env.tracker.status_entity, env.tracker.energy_yesterday_entity, ["running"]
    )
    restarted._store._data = stored
    await restarted.async_start()
    await restarted._refresh_task
    assert list(sums(env).values())[-1] == pytest.approx(100.543)
    await restarted.async_stop()


@pytest.mark.asyncio
async def test_missing_intent_file_prevents_recorder_import(env, monkeypatch):
    monkeypatch.setattr(env.tracker._store, "async_save", AsyncMock())
    today(env, 543)
    await env.tracker._async_refresh()
    assert not env.db._added_calls


@pytest.mark.parametrize("transition", ["running", "end"])
@pytest.mark.asyncio
async def test_status_time_survives_lock_delay_across_midnight(env, transition):
    changed = dt("2026-10-08T23:59:59-04:00")
    env.now[0] = changed
    env.tracker._current_session_start = (
        dt("2026-10-08T23:30:00-04:00") if transition == "end" else None
    )
    await env.tracker._lock.acquire()
    event = integration.Event({"new_state": state(transition, changed.isoformat())})
    task = asyncio.create_task(env.tracker._async_on_status_change(event))
    await asyncio.sleep(0)
    env.now[0] = dt("2026-10-09T00:00:05-04:00")
    env.tracker._lock.release()
    await task
    await env.tracker._refresh_task
    if transition == "end":
        assert dt(env.tracker._sessions[-1]["end"]) == changed
        assert len(env.tracker._sessions_for_day("2026-10-08")) == 2
    else:
        assert env.tracker._current_session_start == changed


@pytest.mark.parametrize("response", ["correct", "wrong_date", "missing", "api_failure"])
@pytest.mark.asyncio
async def test_cached_midnight_zero_cannot_erase_prior_energy(env, response):
    today(env, 543)
    await env.tracker._async_refresh()
    env.now[0] = dt("2026-10-09T00:01:00-04:00")
    # An ordinary coordinator publication advances last_reported, without fetching energy.
    env.values["sensor.dryer_energy_yesterday"] = state(0, env.now[0].isoformat())
    entity = add_lg_entity(env.hass)
    api = entity.coordinator.api.async_get_energy_usage
    api.return_value = {
        "correct": [{"usedDate": "20261008", "energyUsage": 543}],
        "wrong_date": [{"usedDate": "20261007", "energyUsage": 0}],
        "missing": [],
        "api_failure": [],
    }[response]
    if response == "api_failure":
        api.side_effect = RuntimeError("LG unavailable")
    env.tracker._yesterday_source = YesterdaySource(env.hass, "sensor.dryer_energy_yesterday")
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(0.543)
    assert hours(env)["2026-10-08T12:00:00+00:00"] == pytest.approx(0.543)
    assert env.tracker._ledger["days"]["2026-10-08"]["final"] is (response == "correct")


@pytest.mark.asyncio
async def test_failed_yesterday_check_does_not_block_today(env):
    entity = add_lg_entity(env.hass)
    entity.coordinator.api.async_get_energy_usage.side_effect = RuntimeError("LG unavailable")
    env.tracker._yesterday_source = YesterdaySource(env.hass, "sensor.dryer_energy_yesterday")
    today(env, 543)
    await env.tracker._async_refresh()
    assert list(sums(env).values())[-1] == pytest.approx(0.543)

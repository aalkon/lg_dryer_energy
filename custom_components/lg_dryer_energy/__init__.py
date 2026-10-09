"""Estimate dryer energy timing from LG totals and observed run sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, UnitOfEnergy
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .ledger import combined_hours, compact, hour_key, timestamp, update_day
from .source import YesterdaySource, native_energy_entity

_LOGGER = logging.getLogger(__name__)
DOMAIN = "lg_dryer_energy"
STATISTIC_ID = f"{DOMAIN}:dryer_energy_attributed"
STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 3
SESSION_RETENTION_DAYS = 14
RECORDER_TIMEOUT_SECONDS = 30
_NON_NUMERIC_STATES = frozenset({"unknown", "unavailable", "none", ""})
DEFAULT_STATUS_ENTITY = "sensor.dryer_current_status"
DEFAULT_ENERGY_YESTERDAY_ENTITY = "sensor.dryer_energy_yesterday"
DEFAULT_ENERGY_TODAY_ENTITY = "sensor.dryer_energy_today"
DEFAULT_ACTIVE_STATES = ["running", "cooling"]
DEFAULT_TOTAL_TIME_ENTITY = "sensor.dryer_total_time"
DEFAULT_REMAINING_TIME_ENTITY = "sensor.dryer_remaining_time"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up one dryer, preserving the existing external statistic ID."""
    conf = config.get(DOMAIN, {})
    tracker = DryerSessionTracker(
        hass,
        conf.get("status_entity", DEFAULT_STATUS_ENTITY),
        conf.get("energy_yesterday_entity", DEFAULT_ENERGY_YESTERDAY_ENTITY),
        conf.get("active_states", DEFAULT_ACTIVE_STATES),
        total_time_entity=conf.get("total_time_entity", DEFAULT_TOTAL_TIME_ENTITY),
        remaining_time_entity=conf.get("remaining_time_entity", DEFAULT_REMAINING_TIME_ENTITY),
        energy_today_entity=conf.get("energy_today_entity", DEFAULT_ENERGY_TODAY_ENTITY),
        migration_last_processed_date=conf.get("migration_last_processed_date"),
    )
    await tracker.async_start()
    hass.data[DOMAIN] = tracker
    return True


class _LgDryerStore(Store):
    async def async_save_verified(self, data):
        """Read the actual file: Store logs some write errors without raising.

        Store.async_load can return pending/cached data, so it cannot establish
        that an accounting intent survived a disk write failure.
        """
        snapshot = deepcopy(data)
        await self.async_save(snapshot)
        if self.hass.state is CoreState.stopping:
            return  # HA will persist the intent at its final-write event.
        saved = await self.hass.async_add_executor_job(self._read_saved_data)
        if self.hass.state is CoreState.stopping:
            return
        if (
            saved.get("version") != self.version
            or saved.get("key") != self.key
            or saved.get("data") != snapshot
        ):
            raise OSError("Dryer accounting storage did not persist the expected data")

    def _read_saved_data(self):
        return json.loads(Path(self.path).read_text(encoding="utf-8"))

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        """Keep v1/v2 sessions and the last fully processed reporting date."""
        old_data.setdefault("last_processed_local_date", None)
        old_data.setdefault("ledger", None)
        return old_data


def _energy_wh(state) -> float | None:
    """Reject missing/invalid readings; a genuine zero is not unavailable."""
    if state is None or getattr(state, "attributes", {}).get("restored"):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    unit = getattr(state, "attributes", {}).get("unit_of_measurement", "Wh")
    if unit not in ("Wh", "kWh") or not math.isfinite(value) or value < 0:
        return None
    return value * (1000 if unit == "kWh" else 1)


class DryerSessionTracker:
    """Serialize session changes and durable, replaceable daily allocations."""

    def __init__(
        self,
        hass,
        status_entity,
        energy_yesterday_entity,
        active_states,
        total_time_entity=DEFAULT_TOTAL_TIME_ENTITY,
        remaining_time_entity=DEFAULT_REMAINING_TIME_ENTITY,
        energy_today_entity=DEFAULT_ENERGY_TODAY_ENTITY,
        migration_last_processed_date=None,
    ):
        self.hass = hass
        self.status_entity = status_entity
        self.energy_yesterday_entity = energy_yesterday_entity
        self.energy_today_entity = energy_today_entity
        self.active_states = [s.lower() for s in active_states]
        self.total_time_entity = total_time_entity
        self.remaining_time_entity = remaining_time_entity
        self._migration_date = (
            date.fromisoformat(str(migration_last_processed_date)).isoformat()
            if migration_last_processed_date is not None
            else None
        )
        self._migration_retry_at = None
        self._migration_warned = False
        self._store = _LgDryerStore(hass, STORAGE_VERSION, STORAGE_KEY, atomic_writes=True)
        self._yesterday_source = YesterdaySource(hass, energy_yesterday_entity)
        self._sessions: list[dict] = []
        self._current_session_start: datetime | None = None
        self._last_processed_local_date: str | None = None
        self._ledger: dict | None = None
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._unsubscribers = []
        self._refresh_task = None
        self._stopped = False

    async def async_start(self):
        stored = await self._store.async_load() or {}
        self._sessions = stored.get("sessions", [])
        self._last_processed_local_date = stored.get("last_processed_local_date")
        self._ledger = stored.get("ledger")
        if self._ledger is None and not self._last_processed_local_date:
            self._last_processed_local_date = self._migration_date
        if start := stored.get("current_session_start"):
            self._current_session_start = timestamp(start)

        async with self._lock:
            # Subscribe before any snapshot-related await; queued events retain
            # their original timestamps and run after initialization finishes.
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, [self.status_entity], self._async_on_status_change
                )
            )
            try:
                state = self.hass.states.get(self.status_entity)
                if state and state.state.lower() in self.active_states:
                    if self._current_session_start is None:
                        now = dt_util.utcnow()
                        self._current_session_start = (
                            self._resume_from_lg_sensors(now)
                            or await self._async_reconstruct_session_start()
                            or now
                        )
                elif state and state.state.lower() not in _NON_NUMERIC_STATES:
                    self._close_session(getattr(state, "last_changed", dt_util.utcnow()))
                self._prune_sessions()
                await self._async_save()
            except BaseException:
                self._unsubscribers.pop()()
                raise
        energy_entities = [self.energy_yesterday_entity]
        if self.energy_today_entity:
            energy_entities.append(self.energy_today_entity)
        self._unsubscribers.append(
            async_track_state_change_event(self.hass, energy_entities, self._queue_refresh)
        )
        # The minute timer checks cached today data and drives the separately
        # throttled, date-verified yesterday read (including equal daily totals).
        self._unsubscribers.append(
            async_track_time_interval(self.hass, self._queue_refresh, timedelta(minutes=1))
        )
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.async_stop)
        # Recorder does not process its write queue until HA has started.
        # Never await a statistics import from integration setup.
        self._unsubscribers.append(async_at_started(self.hass, self._queue_refresh))

    async def async_stop(self, event=None):
        self._stopped = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._refresh_task:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task
        async with self._lock:
            # HA defers Store writes during shutdown until its final-write event.
            await self._async_save(verify=False)

    @callback
    def _queue_refresh(self, event=None):
        if (
            not self._stopped
            and self.hass.state is CoreState.running
            and (self._refresh_task is None or self._refresh_task.done())
        ):
            self._refresh_task = self.hass.async_create_task(self._async_refresh())

    def _close_session(self, end):
        if self._current_session_start is None:
            return
        start = self._current_session_start
        if timedelta(0) < end - start <= timedelta(days=1):
            self._sessions.append({"start": start.isoformat(), "end": end.isoformat()})
        else:
            _LOGGER.warning("Ignoring implausible dryer session: %s to %s", start, end)
        self._current_session_start = None
        self._prune_sessions()

    def _prune_sessions(self):
        """Bound history even when no energy can be attributed; warn only once."""
        cutoff = dt_util.utcnow() - timedelta(days=SESSION_RETENTION_DAYS)
        retained = []
        for session in self._sessions:
            try:
                start, end = timestamp(session["start"]), timestamp(session["end"])
                if not timedelta(0) < end - start <= timedelta(days=1):
                    raise ValueError("Invalid session duration")
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ignoring implausible dryer session: %s", session)
                continue
            if end >= cutoff:
                retained.append(session)
        changed = retained != self._sessions
        self._sessions = retained
        return changed

    async def _async_on_status_change(self, event: Event):
        state = event.data.get("new_state")
        if state is None or state.state.lower() in _NON_NUMERIC_STATES:
            return  # A connectivity flap is not a cycle completion.
        changed = getattr(state, "last_changed", None) or getattr(event, "time_fired", None)
        if not isinstance(changed, datetime):
            changed = dt_util.utcnow()
        async with self._lock:
            previous_start = self._current_session_start
            if state.state.lower() in self.active_states:
                if self._current_session_start is None:
                    self._current_session_start = changed
            else:
                self._close_session(changed)
            if self._current_session_start == previous_start:
                return
            await self._async_save()
        self._queue_refresh()

    async def _async_refresh(self):
        # Keep source reads serialized, without blocking status persistence.
        async with self._refresh_lock:
            await self._async_refresh_once()

    async def _async_refresh_once(self):
        if self._stopped or self.hass.state is not CoreState.running:
            return
        now = dt_util.utcnow()
        yesterday = (dt_util.as_local(now) - timedelta(days=1)).date()
        value = await self._yesterday_source.async_read(
            yesterday, now, _energy_wh(self.hass.states.get(self.energy_yesterday_entity))
        )
        async with self._lock:
            if self._stopped or self.hass.state is not CoreState.running:
                return
            try:
                if self._prune_sessions():
                    await self._async_save()
                if self._ledger and self._ledger.get("dirty"):
                    await self._async_flush()
                local_now = dt_util.as_local(dt_util.utcnow())
                if value is not None:
                    await self._async_record(yesterday.isoformat(), value, final=True)

                state = (
                    self.hass.states.get(self.energy_today_entity)
                    if self.energy_today_entity
                    and native_energy_entity(self.hass, self.energy_today_entity, "today")
                    else None
                )
                value = _energy_wh(state)
                if value is not None:
                    reset = getattr(state, "attributes", {}).get("last_reset")
                    try:
                        reset = timestamp(reset) if isinstance(reset, str) else reset
                        day = (
                            dt_util.as_local(reset).date() if isinstance(reset, datetime) else None
                        )
                    except (ValueError, TypeError):
                        day = None
                    # Never guess which day a stale pre-midnight total covers.
                    if day == local_now.date() and self._current_session_start is None:
                        await self._async_record(day.isoformat(), value, final=False)
            except Exception:
                _LOGGER.exception("Energy attribution failed; persisted work will be retried")

    def _sessions_for_day(self, day):
        result = []
        for session in self._sessions:
            if not session.get("end"):
                continue
            start, end = timestamp(session["start"]), timestamp(session["end"])
            if end <= start or end - start > timedelta(days=1):
                continue
            if dt_util.as_local(end).date().isoformat() == day:
                result.append({"start": start.isoformat(), "end": end.isoformat()})
        return result

    async def _async_record(self, day: str, total_wh: float, *, final: bool):
        """Replace one reporting day's contribution and rebuild later sums."""
        if not math.isfinite(total_wh) or total_wh < 0:
            return
        if self._last_processed_local_date and day <= self._last_processed_local_date:
            return  # Already included in migrated v2 statistics.
        sessions = self._sessions_for_day(day)
        if self._ledger is None:
            if total_wh == 0:
                return
            if not await self._async_initialize_ledger():
                return
        local_now = dt_util.as_local(dt_util.utcnow())
        noon = datetime.fromisoformat(day).replace(hour=12, tzinfo=local_now.tzinfo)
        previous = self._ledger["days"].get(day)
        updated = update_day(previous, total_wh, sessions, final=final, fallback=noon)
        if previous == updated:
            return
        candidate = deepcopy(self._ledger)
        candidate["days"][day] = updated
        candidate["written_hours"] = sorted(combined_hours(candidate))
        cutoff = dt_util.utcnow() - timedelta(days=SESSION_RETENTION_DAYS + 2)
        compact(candidate, cutoff)
        candidate["dirty"] = True
        # Durable intent precedes enqueue. Replays replace identical rows and
        # all later cumulative sums, even after a crash between storage/DB.
        old = self._ledger
        self._ledger = candidate
        try:
            await self._async_save()
        except Exception:
            self._ledger = old
            raise
        if self.hass.state is CoreState.running and not self._stopped:
            await self._async_flush()

    async def _async_read_statistics(self, start, end=None):
        return (
            await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                {STATISTIC_ID},
                "hour",
                {"energy": "kWh"},
                {"sum"},
            )
        ).get(STATISTIC_ID, [])

    async def _async_initialize_ledger(self):
        # One-time migration includes arbitrarily long gaps. A failed query
        # must never reset an existing cumulative series to zero.
        now = dt_util.utcnow()
        if self._migration_retry_at and now < self._migration_retry_at:
            return False
        self._migration_retry_at = now + timedelta(hours=1)
        async with asyncio.timeout(RECORDER_TIMEOUT_SECONDS):
            rows = await self._async_read_statistics(datetime(1970, 1, 1, tzinfo=UTC))
        if rows and not self._last_processed_local_date:
            if not self._migration_warned:
                _LOGGER.error(
                    "Existing dryer statistics have no migration marker; restore matching storage "
                    "or configure migration_last_processed_date (see README recovery instructions). "
                    "Attribution is paused; retrying hourly."
                )
                self._migration_warned = True
            return False
        anchor = hour_key(dt_util.utcnow() - timedelta(days=SESSION_RETENTION_DAYS + 2))
        base_sum = previous = 0.0
        legacy = {}
        for row in sorted(rows, key=lambda row: row["start"]):
            if row.get("sum") is None:
                continue
            key = hour_key(datetime.fromtimestamp(row["start"], UTC))
            value = float(row["sum"])
            if key < anchor:
                base_sum = value
            else:
                legacy[key] = value - previous
            previous = value
        self._ledger = {
            "anchor": anchor,
            "base_sum": base_sum,
            "legacy_hours": legacy,
            "days": {},
            "written_hours": list(legacy),
            "dirty": False,
        }
        self._migration_retry_at = None
        return True

    async def _async_flush(self):
        ledger = self._ledger
        hours = combined_hours(ledger)
        running = ledger["base_sum"]
        anchor = timestamp(ledger["anchor"])
        statistics = [StatisticData(start=anchor - timedelta(hours=1), state=0.0, sum=running)]
        for key in sorted(hours):
            running += hours[key]
            statistics.append(StatisticData(start=timestamp(key), state=hours[key], sum=running))
        async_add_external_statistics(
            self.hass,
            {
                "mean_type": StatisticMeanType.NONE,
                "has_sum": True,
                "name": "Dryer Energy (Attributed)",
                "source": DOMAIN,
                "statistic_id": STATISTIC_ID,
                "unit_class": "energy",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            },
            statistics,
        )
        async with asyncio.timeout(RECORDER_TIMEOUT_SECONDS):
            await get_instance(self.hass).async_block_till_done()
            actual = {
                row["start"]: row.get("sum")
                for row in await self._async_read_statistics(anchor - timedelta(hours=1))
            }
        for row in statistics:
            value = actual.get(row["start"].timestamp())
            if value is None or not math.isclose(value, row["sum"], abs_tol=1e-8):
                raise RuntimeError("Recorder has not committed the expected dryer statistics")
        ledger["dirty"] = False
        self._prune_sessions()
        await self._async_save()

    async def _async_save(self, *, verify=True):
        save = self._store.async_save_verified if verify else self._store.async_save
        await save(
            {
                "sessions": self._sessions,
                "current_session_start": self._current_session_start.isoformat()
                if self._current_session_start
                else None,
                "last_processed_local_date": self._last_processed_local_date,
                "ledger": self._ledger,
            }
        )

    def _resume_from_lg_sensors(self, now: datetime) -> datetime | None:
        """Reconstruct session start from LG's total_time and remaining_time sensors.

        These sensors reflect the dryer's internal cycle clock and survive HA
        restarts and recorder purges. Returns the reconstructed start, or None
        if the sensors are unavailable, non-numeric, inconsistent, or in the
        cooling phase (where remaining_time is 0 but cooling time is not counted
        in total_time, making the derived elapsed incorrect).
        """
        total_state = self.hass.states.get(self.total_time_entity)
        remaining_state = self.hass.states.get(self.remaining_time_entity)
        if not total_state or not remaining_state:
            return None

        total_raw = (total_state.state or "").lower()
        remaining_raw = (remaining_state.state or "").lower()
        if total_raw in _NON_NUMERIC_STATES or remaining_raw in _NON_NUMERIC_STATES:
            return None

        try:
            total_min = float(total_state.state)
            remaining_min = float(remaining_state.state)
        except (ValueError, TypeError):
            return None

        # Sanity: remaining cannot exceed total, and total must be positive.
        if (
            not math.isfinite(total_min)
            or not math.isfinite(remaining_min)
            or total_min <= 0
            or remaining_min < 0
            or remaining_min > total_min
        ):
            return None

        # During cooling, LG typically reports remaining_time == 0 while the
        # dryer is still in an active state. But total_time is the programmed
        # cycle length excluding cooling, so (total - 0) is NOT the real
        # elapsed. Skip this tier in cooling and let the history walk handle it.
        status_state = self.hass.states.get(self.status_entity)
        if status_state and (status_state.state or "").lower() == "cooling" and remaining_min == 0:
            return None

        elapsed_min = total_min - remaining_min
        if elapsed_min <= 0 or elapsed_min > 24 * 60:
            return None

        return now - timedelta(minutes=elapsed_min)

    async def _async_reconstruct_session_start(self) -> datetime | None:
        """Reconstruct the true session start from recorder history.

        Called on startup when the status entity is already in an active
        state. Walks backward through the most recent state changes and
        returns the `last_changed` timestamp of the earliest state in the
        current contiguous run of active states. Returns None if the
        recorder history module is unavailable, the query fails, the
        result is empty, or the most recent recorded state is not active
        (in which case the current liveness is a post-restart restore and
        we cannot trust history to establish a true start).
        """
        try:
            from homeassistant.components.recorder import (  # noqa: WPS433
                history as recorder_history,
            )
        except ImportError:
            _LOGGER.debug(
                "Recorder history module unavailable; cannot reconstruct session start from history"
            )
            return None

        get_last_state_changes = getattr(recorder_history, "get_last_state_changes", None)
        if get_last_state_changes is None:
            _LOGGER.debug(
                "get_last_state_changes not present on recorder.history; "
                "cannot reconstruct session start"
            )
            return None

        try:
            changes = await get_instance(self.hass).async_add_executor_job(
                get_last_state_changes,
                self.hass,
                20,
                self.status_entity,
            )
        except Exception:
            _LOGGER.exception("get_last_state_changes failed; cannot reconstruct session start")
            return None

        states = (changes or {}).get(self.status_entity) or []
        if not states:
            return None

        def _ts(s: Any) -> datetime | None:
            return getattr(s, "last_changed", None) or getattr(s, "last_updated", None)

        # Order ascending by timestamp so the last element is the most recent.
        try:
            ordered = sorted(
                (s for s in states if _ts(s) is not None),
                key=_ts,
            )
        except TypeError:
            ordered = list(states)

        if not ordered:
            return None

        latest = ordered[-1]
        latest_val = (getattr(latest, "state", "") or "").lower()
        if latest_val not in self.active_states:
            # The most recent recorded state is not active; cannot
            # confidently locate the current run from history.
            return None

        earliest_active_ts: datetime | None = _ts(latest)
        for s in reversed(ordered[:-1]):
            val = (getattr(s, "state", "") or "").lower()
            if val in self.active_states:
                ts = _ts(s)
                if ts is not None:
                    earliest_active_ts = ts
            else:
                break

        return earliest_active_ts

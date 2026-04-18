"""
LG Dryer Energy Attribution

Tracks dryer run sessions from sensor.dryer_current_status and, when
sensor.dryer_energy_yesterday updates each morning, proportionally
distributes yesterday's energy across the recorded sessions. The energy
is injected into Home Assistant's long-term statistics via
async_add_external_statistics, backdated to each session's actual hour.

This means the Energy Dashboard shows dryer energy in the correct time
buckets rather than lumped at the morning update time.

Installation:
  1. Copy this folder to custom_components/lg_dryer_energy/
  2. Add to configuration.yaml (see CONFIG below)
  3. Restart Home Assistant
  4. In Energy Dashboard → Individual Devices, look for
     "lg_dryer_energy:dryer_energy_attributed"

Configuration (configuration.yaml):
  lg_dryer_energy:
    status_entity: sensor.dryer_current_status
    energy_yesterday_entity: sensor.dryer_energy_yesterday
    active_states:
      - running
      - cooling
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = "lg_dryer_energy"
STATISTIC_ID = f"{DOMAIN}:dryer_energy_attributed"
STORAGE_KEY = f"{DOMAIN}.sessions"
STORAGE_VERSION = 2

# Session retention window. Any session whose local end-date is strictly older
# than `last_processed_local_date` is considered fully handled. Sessions whose
# end-date is newer are preserved, capped by this outer window to prevent
# unbounded growth if attribution stops running for a long time.
SESSION_RETENTION_DAYS = 14

# State values that must be treated as "no reading" rather than a numeric
# transition. A flap into/out of these states is not a new event.
_NON_NUMERIC_STATES = frozenset({"unknown", "unavailable", "none", ""})

# Default configuration
DEFAULT_STATUS_ENTITY = "sensor.dryer_current_status"
DEFAULT_ENERGY_YESTERDAY_ENTITY = "sensor.dryer_energy_yesterday"
DEFAULT_ACTIVE_STATES = ["running", "cooling"]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the LG Dryer Energy Attribution integration."""

    conf = config.get(DOMAIN, {})
    status_entity = conf.get("status_entity", DEFAULT_STATUS_ENTITY)
    energy_yesterday_entity = conf.get(
        "energy_yesterday_entity", DEFAULT_ENERGY_YESTERDAY_ENTITY
    )
    active_states = conf.get("active_states", DEFAULT_ACTIVE_STATES)

    tracker = DryerSessionTracker(
        hass, status_entity, energy_yesterday_entity, active_states
    )
    await tracker.async_start()

    hass.data[DOMAIN] = tracker
    return True


class _LgDryerStore(Store):
    """Store subclass that migrates v1 -> v2 by adding last_processed_local_date."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        if old_major_version < 2:
            old_data.setdefault("last_processed_local_date", None)
        return old_data


class DryerSessionTracker:
    """
    Tracks dryer run sessions and attributes energy to them.

    A "session" is a contiguous period where dryer_current_status is in
    one of the active_states (running, cooling). We record the start and
    end timestamps. When energy_yesterday updates (the LG cloud morning push),
    we look at yesterday's sessions, proportionally split the reported
    Wh across them by duration, and inject statistics rows backdated to
    each session's hour(s).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        status_entity: str,
        energy_yesterday_entity: str,
        active_states: list[str],
    ) -> None:
        self.hass = hass
        self.status_entity = status_entity
        self.energy_yesterday_entity = energy_yesterday_entity
        self.active_states = [s.lower() for s in active_states]

        # Persistent storage for sessions surviving restarts
        self._store = _LgDryerStore(hass, STORAGE_VERSION, STORAGE_KEY)
        self._sessions: list[dict] = []  # {start: isoformat, end: isoformat|None}
        self._current_session_start: datetime | None = None

        # Running sum kept only for diagnostic/backwards-compat purposes.
        # The authoritative baseline is always re-derived from the
        # statistics database at attribution time.
        self._cumulative_kwh: float = 0.0

        # ISO-date (YYYY-MM-DD, local) of the last successfully attributed
        # day. Used as the primary idempotency guard to prevent a duplicate
        # attribution when energy_yesterday flaps through unknown/unavailable.
        self._last_processed_local_date: str | None = None

    async def async_start(self) -> None:
        """Load persisted state and start listening."""
        stored = await self._store.async_load()
        if stored:
            self._sessions = stored.get("sessions", []) or []
            self._cumulative_kwh = stored.get("cumulative_kwh", 0.0) or 0.0
            start_raw = stored.get("current_session_start")
            if start_raw:
                self._current_session_start = datetime.fromisoformat(start_raw)
            # v2 key: may be missing for storage files that predate the
            # migration or were manually edited.
            self._last_processed_local_date = stored.get(
                "last_processed_local_date"
            )
            _LOGGER.info(
                "Loaded %d stored sessions, cumulative=%.3f kWh, last_processed=%s",
                len(self._sessions),
                self._cumulative_kwh,
                self._last_processed_local_date,
            )

        # If we missed the dryer starting while HA was down, check
        # the current state now
        state = self.hass.states.get(self.status_entity)
        if state and state.state.lower() in self.active_states:
            if self._current_session_start is None:
                self._current_session_start = dt_util.utcnow()
                _LOGGER.info("Dryer already active on startup, recording session start")

        # Listen for status changes (session start/end)
        async_track_state_change_event(
            self.hass, [self.status_entity], self._async_on_status_change
        )

        # Listen for energy_yesterday changes (morning update)
        async_track_state_change_event(
            self.hass,
            [self.energy_yesterday_entity],
            self._async_on_energy_yesterday_change,
        )

    @callback
    def _async_on_status_change(self, event: Event) -> None:
        """Handle dryer status transitions."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        now = dt_util.utcnow()
        is_active = new_state.state.lower() in self.active_states

        if is_active and self._current_session_start is None:
            # Session starting
            self._current_session_start = now
            _LOGGER.debug("Dryer session started at %s", now.isoformat())

        elif not is_active and self._current_session_start is not None:
            # Session ending
            session = {
                "start": self._current_session_start.isoformat(),
                "end": now.isoformat(),
            }
            self._sessions.append(session)
            duration = (now - self._current_session_start).total_seconds()
            _LOGGER.info(
                "Dryer session ended: %s → %s (%.0f min)",
                self._current_session_start.isoformat(),
                now.isoformat(),
                duration / 60,
            )
            self._current_session_start = None
            self.hass.async_create_task(self._async_save())

    @callback
    def _async_on_energy_yesterday_change(self, event: Event) -> None:
        """Handle energy_yesterday sensor update (morning push from LG cloud).

        LG ThinQ sensors routinely flap through `unknown` and `unavailable`.
        A transition FROM one of those states BACK to the prior numeric value
        is not a new event and must not trigger re-attribution. This was the
        root cause of the duplicate-attribution bug: catching the ValueError
        from float("unknown") and falling through into the write path.
        """
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return

        new_raw = (new_state.state or "").lower()
        if new_raw in _NON_NUMERIC_STATES:
            _LOGGER.debug(
                "energy_yesterday new_state is non-numeric (%s), ignoring",
                new_raw,
            )
            return

        # A transition OUT OF unknown/unavailable back to a previously-seen
        # numeric value is not a new LG push. Reject it before touching the
        # write path. The daily idempotency guard in _async_attribute_energy
        # provides a second layer of defense.
        if old_state is not None:
            old_raw = (old_state.state or "").lower()
            if old_raw in _NON_NUMERIC_STATES:
                _LOGGER.debug(
                    "energy_yesterday transitioned from %s back to numeric; "
                    "treating as flap recovery, not a new event",
                    old_raw,
                )
                return

        try:
            new_wh = float(new_state.state)
        except (ValueError, TypeError):
            _LOGGER.debug(
                "energy_yesterday new_state %r is not parseable as float",
                new_state.state,
            )
            return

        if new_wh <= 0:
            _LOGGER.debug("energy_yesterday is 0 or negative, skipping")
            return

        # Secondary numeric-equality guard. This is no longer the primary
        # defense, but catches the case where both states are numeric and
        # identical (e.g., a redundant state-write by the ThinQ integration).
        if old_state is not None:
            try:
                if float(old_state.state) == new_wh:
                    return
            except (ValueError, TypeError):
                # old_state was numeric-like but malformed. Continue to
                # the daily idempotency check inside _async_attribute_energy
                # rather than blindly reprocessing.
                pass

        _LOGGER.info(
            "energy_yesterday updated to %.0f Wh, processing attribution", new_wh
        )

        # Schedule the async attribution work
        self.hass.async_create_task(
            self._async_attribute_energy(new_wh)
        )

    async def _async_attribute_energy(self, total_wh: float) -> None:
        """
        Distribute yesterday's total Wh across yesterday's dryer sessions,
        proportional to each session's duration, and inject into statistics.

        Idempotent by local date: a repeated call for the same day is a no-op.
        Baseline is re-derived each call from the statistics database as of
        the moment yesterday began, so two calls for the same day produce
        byte-identical StatisticData lists.
        """
        now = dt_util.utcnow()
        # "Yesterday" in local time
        local_now = dt_util.as_local(now)
        yesterday_start = (
            local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=1)
        )
        yesterday_end = yesterday_start + timedelta(days=1)
        yesterday_date_iso = yesterday_start.date().isoformat()

        # --- Fix #1: idempotency by local date -------------------------------
        if self._last_processed_local_date == yesterday_date_iso:
            _LOGGER.debug(
                "Already processed %s, skipping duplicate attribution for %.0f Wh",
                yesterday_date_iso,
                total_wh,
            )
            return

        # Convert to UTC for comparison
        yesterday_start_utc = dt_util.as_utc(yesterday_start)
        yesterday_end_utc = dt_util.as_utc(yesterday_end)

        # --- Fix #8: attribute by session end-date (LG's model) --------------
        # Empirically verified (April 2026): LG reports each cycle's energy
        # under the local date the cycle ENDED on, not the date it started.
        # A cycle running 23:27 local D-1 -> 00:19 local D shows up as 0 on
        # D-1 and as the full cycle energy on D.
        #
        # Therefore: a session belongs to yesterday's attribution if and only
        # if its end timestamp, converted to local time, falls on
        # yesterday_date_iso. We use the session's FULL unclipped duration for
        # proportional splitting, and when laying down hourly rows we allow
        # them to fall on date D-1 (the day before yesterday) for the
        # pre-midnight portion of an overnight cycle. Those rows are real
        # energy use and are correct even though LG's D-1 daily total is 0.
        #
        # A session whose end-local-date is AFTER yesterday (typically today,
        # for a cycle that crossed midnight into today) is preserved for its
        # own future attribution pass. A session whose end-local-date is
        # strictly BEFORE yesterday has missed its window and is logged and
        # dropped.
        yesterday_local_date = yesterday_start.date()
        retention_cutoff_utc = yesterday_start_utc - timedelta(
            days=SESSION_RETENTION_DAYS
        )
        yesterday_sessions: list[dict] = []
        remaining_sessions: list[dict] = []

        for session in self._sessions:
            s_start = datetime.fromisoformat(session["start"])
            s_end_raw = session.get("end")
            if not s_end_raw:
                _LOGGER.warning(
                    "Persisted session missing end timestamp, skipping: %r",
                    session,
                )
                continue
            s_end = datetime.fromisoformat(s_end_raw)
            if s_start.tzinfo is None:
                s_start = s_start.replace(tzinfo=timezone.utc)
            if s_end.tzinfo is None:
                s_end = s_end.replace(tzinfo=timezone.utc)

            end_local_date = dt_util.as_local(s_end).date()

            if end_local_date == yesterday_local_date:
                # Belongs to this attribution pass. Use full duration.
                yesterday_sessions.append(
                    {
                        "start": s_start,
                        "end": s_end,
                        "duration": (s_end - s_start).total_seconds(),
                    }
                )
            elif end_local_date > yesterday_local_date:
                # Ended on today (or later, if clock skew). Preserve for a
                # future attribution pass once LG reports it.
                remaining_sessions.append(session)
            else:
                # end_local_date < yesterday: session's attribution day has
                # already passed. Drop with appropriate logging.
                if s_end < retention_cutoff_utc:
                    _LOGGER.warning(
                        "Dropping session ending %s, older than %d-day "
                        "retention window and never attributed",
                        s_end.isoformat(),
                        SESSION_RETENTION_DAYS,
                    )
                else:
                    _LOGGER.warning(
                        "Session %s -> %s ended on local date %s which is "
                        "older than yesterday (%s); never attributed (HA "
                        "likely down on its attribution day)",
                        session["start"],
                        s_end_raw,
                        end_local_date.isoformat(),
                        yesterday_date_iso,
                    )

        # A session that is still in progress (self._current_session_start set,
        # no end recorded) cannot be attributed yet: LG only reports energy
        # after a cycle completes. Do NOT synthesize a partial session into
        # yesterday's attribution; it will be picked up once the cycle ends
        # and energy_yesterday fires for its local end-date.

        if not yesterday_sessions:
            _LOGGER.warning(
                "Got %.0f Wh for yesterday but found no dryer sessions. "
                "Attributing entire amount to noon yesterday as fallback.",
                total_wh,
            )
            # Fallback: put it all at noon yesterday
            noon = yesterday_start_utc + timedelta(hours=12)
            yesterday_sessions = [
                {"start": noon, "end": noon + timedelta(minutes=1), "duration": 60}
            ]

        total_duration = sum(s["duration"] for s in yesterday_sessions)
        if total_duration <= 0:
            _LOGGER.error("Total session duration is 0, cannot attribute energy")
            return

        total_kwh = total_wh / 1000.0

        _LOGGER.info(
            "Attributing %.3f kWh across %d sessions (%.0f min total)",
            total_kwh,
            len(yesterday_sessions),
            total_duration / 60,
        )

        # Build hourly energy buckets
        hourly_kwh: dict[datetime, float] = {}

        for session in yesterday_sessions:
            session_kwh = total_kwh * (session["duration"] / total_duration)
            session_start: datetime = session["start"]
            session_end: datetime = session["end"]

            # Split this session's energy across the hours it spans
            # Ensure UTC timezone is set for arithmetic
            if session_start.tzinfo is None:
                session_start = session_start.replace(tzinfo=timezone.utc)
            if session_end.tzinfo is None:
                session_end = session_end.replace(tzinfo=timezone.utc)

            hour_cursor = session_start.replace(
                minute=0, second=0, microsecond=0
            )
            while hour_cursor < session_end:
                hour_end = hour_cursor + timedelta(hours=1)
                # Overlap between this hour and the session
                overlap_start = max(hour_cursor, session_start)
                overlap_end = min(hour_end, session_end)
                overlap_seconds = (overlap_end - overlap_start).total_seconds()

                if overlap_seconds > 0 and session["duration"] > 0:
                    # Fraction of this session that falls in this hour
                    fraction = overlap_seconds / session["duration"]
                    hourly_kwh[hour_cursor] = hourly_kwh.get(hour_cursor, 0.0) + (
                        session_kwh * fraction
                    )

                hour_cursor = hour_end

        # --- Fix #3/#8: stable baseline from BEFORE the earliest written hour
        # Under the end-date model, an overnight cycle may write an hour that
        # falls on date D-1 (the day before yesterday). The baseline must be
        # fetched strictly before the earliest hour we're about to write, not
        # merely before yesterday_start, so the cumulative sum remains
        # monotonic and replay-idempotent.
        if hourly_kwh:
            earliest_hour_utc = min(hourly_kwh.keys())
        else:
            earliest_hour_utc = yesterday_start_utc
        baseline_sum = await self._async_get_baseline_sum(earliest_hour_utc)

        # Build StatisticData rows sorted by hour
        statistics: list[StatisticData] = []
        running_sum = baseline_sum

        for hour_ts in sorted(hourly_kwh.keys()):
            kwh_this_hour = hourly_kwh[hour_ts]
            running_sum += kwh_this_hour
            statistics.append(
                StatisticData(
                    start=hour_ts,
                    # Fix #7: per-hour delta is the correct `state` for a
                    # has_sum=True series. `sum` is the cumulative.
                    state=kwh_this_hour,
                    sum=running_sum,
                )
            )
            _LOGGER.debug(
                "  %s: +%.4f kWh (cumulative: %.4f)",
                hour_ts.isoformat(),
                kwh_this_hour,
                running_sum,
            )

        # Inject into HA statistics
        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": "Dryer Energy (Attributed)",
            "source": DOMAIN,
            "statistic_id": STATISTIC_ID,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        }

        # --- Fix #4: mutate state only on successful write -------------------
        # async_add_external_statistics is a synchronous callback; if it
        # raises, none of the state below is touched and the next invocation
        # can retry cleanly.
        async_add_external_statistics(self.hass, metadata, statistics)

        _LOGGER.info(
            "Injected %d hourly statistics rows (%.3f kWh total for %s)",
            len(statistics),
            total_kwh,
            yesterday_date_iso,
        )

        self._sessions = remaining_sessions
        self._cumulative_kwh = running_sum
        self._last_processed_local_date = yesterday_date_iso
        await self._async_save()

    async def _async_get_baseline_sum(
        self, before_utc: datetime
    ) -> float:
        """Return the cumulative `sum` of our statistic as of `before_utc`.

        Fetches the newest statistic row strictly before `before_utc`. Used
        to derive a stable baseline so the attribution is idempotent: running
        twice with the same inputs yields identical StatisticData lists, and
        cumulative sums remain monotonic across backdated writes.
        """
        window_start = before_utc - timedelta(days=8)
        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                window_start,
                before_utc,
                {STATISTIC_ID},
                "hour",
                None,
                {"sum"},
            )
        except Exception:  # noqa: BLE001 - defensive, API shape varies by HA version
            _LOGGER.exception(
                "statistics_during_period failed; defaulting baseline to 0.0"
            )
            rows = {}

        series = rows.get(STATISTIC_ID) if rows else None
        if series:
            last_row = series[-1]
            baseline = last_row.get("sum")
            if baseline is not None:
                _LOGGER.debug(
                    "Baseline sum=%.4f from stat row at %s (before %s)",
                    baseline,
                    last_row.get("start"),
                    before_utc.isoformat(),
                )
                return float(baseline)

        _LOGGER.debug(
            "No prior stat row before %s; baseline defaults to 0.0",
            before_utc.isoformat(),
        )
        return 0.0

    async def _async_save(self) -> None:
        """Persist session data and cumulative total."""
        await self._store.async_save(
            {
                "sessions": self._sessions,
                "cumulative_kwh": self._cumulative_kwh,
                "current_session_start": (
                    self._current_session_start.isoformat()
                    if self._current_session_start
                    else None
                ),
                "last_processed_local_date": self._last_processed_local_date,
            }
        )

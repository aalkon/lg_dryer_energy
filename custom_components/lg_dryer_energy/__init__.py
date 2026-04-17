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
    get_last_statistics,
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
STORAGE_VERSION = 1

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
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._sessions: list[dict] = []  # {start: isoformat, end: isoformat|None}
        self._current_session_start: datetime | None = None

        # Running sum for the external statistic
        self._cumulative_kwh: float = 0.0

    async def async_start(self) -> None:
        """Load persisted state and start listening."""
        stored = await self._store.async_load()
        if stored:
            self._sessions = stored.get("sessions", [])
            self._cumulative_kwh = stored.get("cumulative_kwh", 0.0)
            start_raw = stored.get("current_session_start")
            if start_raw:
                self._current_session_start = datetime.fromisoformat(start_raw)
            _LOGGER.info(
                "Loaded %d stored sessions, cumulative=%.3f kWh",
                len(self._sessions),
                self._cumulative_kwh,
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
        """Handle energy_yesterday sensor update (morning push from LG cloud)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return

        try:
            new_wh = float(new_state.state)
        except (ValueError, TypeError):
            return

        if new_wh <= 0:
            _LOGGER.debug("energy_yesterday is 0 or negative, skipping")
            return

        # Avoid re-processing if the value hasn't actually changed
        if old_state is not None:
            try:
                old_wh = float(old_state.state)
                if old_wh == new_wh:
                    return
            except (ValueError, TypeError):
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
        """
        now = dt_util.utcnow()
        # "Yesterday" in local time
        local_now = dt_util.as_local(now)
        yesterday_start = (
            local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=1)
        )
        yesterday_end = yesterday_start + timedelta(days=1)

        # Convert to UTC for comparison
        yesterday_start_utc = dt_util.as_utc(yesterday_start)
        yesterday_end_utc = dt_util.as_utc(yesterday_end)

        # Find sessions that overlap with yesterday
        yesterday_sessions: list[dict] = []
        remaining_sessions: list[dict] = []
        cutoff_utc = yesterday_start_utc - timedelta(days=7)  # GC: drop old sessions

        for session in self._sessions:
            s_start = datetime.fromisoformat(session["start"])
            s_end_raw = session.get("end")
            if s_end_raw:
                s_end = datetime.fromisoformat(s_end_raw)
            else:
                # Session still in progress (shouldn't happen for yesterday)
                s_end = now

            # Garbage collect sessions older than 7 days before yesterday
            if s_end < cutoff_utc:
                continue

            # Does this session overlap with yesterday?
            if s_start < yesterday_end_utc and s_end > yesterday_start_utc:
                # Clip to yesterday's boundaries
                clipped_start = max(s_start, yesterday_start_utc)
                clipped_end = min(s_end, yesterday_end_utc)
                yesterday_sessions.append(
                    {
                        "start": clipped_start,
                        "end": clipped_end,
                        "duration": (clipped_end - clipped_start).total_seconds(),
                    }
                )

            # Keep sessions that end after yesterday (today's sessions, etc.)
            if s_end >= yesterday_end_utc:
                remaining_sessions.append(session)

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

        # Get the current cumulative sum from last statistics entry
        # This ensures continuity with previously injected data
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, STATISTIC_ID, True, {"sum"}
        )

        if last_stats and STATISTIC_ID in last_stats:
            last_sum = last_stats[STATISTIC_ID][0].get("sum", 0.0) or 0.0
        else:
            last_sum = self._cumulative_kwh

        # Build StatisticData rows sorted by hour
        statistics: list[StatisticData] = []
        running_sum = last_sum

        for hour_ts in sorted(hourly_kwh.keys()):
            kwh_this_hour = hourly_kwh[hour_ts]
            running_sum += kwh_this_hour
            statistics.append(
                StatisticData(
                    start=hour_ts,
                    state=running_sum,
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

        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.info(
            "Injected %d hourly statistics rows (%.3f kWh total for yesterday)",
            len(statistics),
            total_kwh,
        )

        # Update our running total and clean up old sessions
        self._cumulative_kwh = running_sum
        self._sessions = remaining_sessions
        await self._async_save()

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
            }
        )

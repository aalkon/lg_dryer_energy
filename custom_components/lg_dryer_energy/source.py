"""Date-verified daily reads using the configured native LG energy entity."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.helpers.entity_component import DATA_INSTANCES

_LOGGER = logging.getLogger(__name__)
YESTERDAY_SETTLE_HOUR = 6


def native_energy_entity(hass, entity_id, key):
    """Require an enabled native LG energy entity for the requested period."""
    from homeassistant.components.lg_thinq.sensor import ThinQEnergySensorEntity

    component = hass.data.get(DATA_INSTANCES, {}).get("sensor")
    entity = component.get_entity(entity_id) if component else None
    if isinstance(entity, ThinQEnergySensorEntity) and entity.entity_description.key == key:
        return entity
    return None


class YesterdaySource:
    """Never infer a source date from an HA entity's publication timestamp.

    Native coordinator updates republish cached energy without fetching it.
    Use the existing authenticated SDK connection, request one explicit date,
    and require a matching detailed row. No credentials are copied or stored.
    """

    def __init__(self, hass, entity_id: str):
        self.hass = hass
        self.entity_id = entity_id
        self._day: date | None = None
        self._value: float | None = None
        self._next_attempt: datetime | None = None
        self._missing_warned = False

    async def async_read(self, day: date, now: datetime, hint: float | None) -> float | None:
        """Wait until local morning, then revalidate hourly regardless of hint.

        The native hint cannot prove freshness or completeness and is ignored.
        Limit queries to one per hour, including across a midnight rollover.
        """
        if day != self._day:
            self._day, self._value = day, None
        local_now = dt_util.as_local(now)
        settle = datetime.combine(day + timedelta(days=1), datetime.min.time()).replace(
            hour=YESTERDAY_SETTLE_HOUR, tzinfo=local_now.tzinfo
        )
        if local_now < settle:
            return None
        if self._next_attempt is not None and now < self._next_attempt:
            return self._value
        component = self.hass.data.get(DATA_INSTANCES, {}).get("sensor")
        entity = component.get_entity(self.entity_id) if component else None
        if entity is None:
            if not self._missing_warned:
                _LOGGER.warning(
                    "LG yesterday entity %s is missing or disabled; check energy_yesterday_entity",
                    self.entity_id,
                )
                self._missing_warned = True
            return self._value  # The native integration may still be loading.
        self._missing_warned = False
        self._next_attempt = now + timedelta(hours=1)
        try:
            # Import lazily so a missing LG integration does not disable today.
            from homeassistant.components.lg_thinq.sensor import ThinQEnergySensorEntity

            if (
                not isinstance(entity, ThinQEnergySensorEntity)
                or entity.entity_description.key != "yesterday"
            ):
                raise ValueError(
                    "energy_yesterday_entity must be a native LG Energy yesterday sensor"
                )
            async with asyncio.timeout(30):
                rows = await entity.coordinator.api.async_get_energy_usage(
                    energy_property=entity.property_id,
                    period=entity.entity_description.usage_period,
                    start_date=day,
                    end_date=day,
                    detail=True,
                )
            self._value = daily_wh(rows, day, entity.property_id)
        except Exception:
            _LOGGER.warning(
                "Could not verify LG energy for %s; retaining prior attribution", day, exc_info=True
            )
        return self._value


def daily_wh(rows, day: date, property_id: str) -> float:
    """Require exactly one explicit daily row; absent data is not zero."""
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Expected one dated daily energy row")
    row = rows[0]
    if not isinstance(row, dict) or row.get("usedDate") != day.strftime("%Y%m%d"):
        raise ValueError("LG daily energy response date does not match the request")
    raw = row.get(property_id)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError("Missing or invalid daily energy value")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Daily energy must be finite and non-negative")
    return value

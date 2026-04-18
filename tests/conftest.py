"""
Stub Home Assistant symbols for unit-testing lg_dryer_energy without a full HA install.

The integration imports a handful of things from `homeassistant.*`. These tests are
narrow unit tests for the attribution logic, not integration tests against a live
HA instance, so we install minimal stubs in sys.modules before the integration is
imported.

If you want full HA-harness tests (e.g., Test 9 "storage migration via Store"),
install `pytest-homeassistant-custom-component` and add parallel tests under this
same directory.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _ensure_module(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# ---- homeassistant.core ----------------------------------------------------
_core = _ensure_module("homeassistant")
_core_mod = _ensure_module("homeassistant.core")


class HomeAssistant:  # pragma: no cover - placeholder
    pass


class Event:  # pragma: no cover - placeholder
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def callback(func):  # pragma: no cover - passthrough decorator
    return func


_core_mod.HomeAssistant = HomeAssistant
_core_mod.Event = Event
_core_mod.callback = callback


# ---- homeassistant.const ---------------------------------------------------
_const_mod = _ensure_module("homeassistant.const")


class UnitOfEnergy(str, Enum):
    KILO_WATT_HOUR = "kWh"


_const_mod.UnitOfEnergy = UnitOfEnergy


# ---- homeassistant.util.dt -------------------------------------------------
_util_mod = _ensure_module("homeassistant.util")
_dt_mod = _ensure_module("homeassistant.util.dt")


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_local(dt: datetime) -> datetime:
    # Tests pin a synthetic local tz via the `local_tz` fixture.
    tz = getattr(_dt_mod, "_LOCAL_TZ", timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


_dt_mod.utcnow = utcnow
_dt_mod.as_utc = as_utc
_dt_mod.as_local = as_local
_dt_mod._LOCAL_TZ = timezone.utc


# ---- homeassistant.helpers.event -------------------------------------------
_helpers_mod = _ensure_module("homeassistant.helpers")
_helpers_event_mod = _ensure_module("homeassistant.helpers.event")


def async_track_state_change_event(hass, entities, handler):  # pragma: no cover
    return lambda: None


_helpers_event_mod.async_track_state_change_event = async_track_state_change_event


# ---- homeassistant.helpers.storage -----------------------------------------
_helpers_storage_mod = _ensure_module("homeassistant.helpers.storage")


class Store:
    """Minimal in-memory Store stub. Tests can read/write via ._data."""

    def __init__(self, hass, version, key, **kwargs) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self._data: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any] | None:
        return self._data

    async def async_save(self, data: dict[str, Any]) -> None:
        self._data = data


_helpers_storage_mod.Store = Store


# ---- homeassistant.components.recorder -------------------------------------
_rec_root = _ensure_module("homeassistant.components")
_rec_mod = _ensure_module("homeassistant.components.recorder")
_rec_models_mod = _ensure_module("homeassistant.components.recorder.models")
_rec_stats_mod = _ensure_module("homeassistant.components.recorder.statistics")


class _RecorderInstance:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


_REC_INSTANCE = _RecorderInstance()


def get_instance(hass):  # pragma: no cover
    return _REC_INSTANCE


_rec_mod.get_instance = get_instance


@dataclass
class StatisticData:
    start: datetime
    state: float
    sum: float


_rec_models_mod.StatisticData = StatisticData


# These are replaced per-test by fixtures; defaults are inert.
def _default_add_external_statistics(hass, metadata, statistics):  # pragma: no cover
    _rec_stats_mod._added_calls.append((metadata, list(statistics)))


def _default_statistics_during_period(
    hass, start_time, end_time, statistic_ids, period, units, types
):  # pragma: no cover
    return dict(_rec_stats_mod._stats_rows)


_rec_stats_mod._added_calls = []
_rec_stats_mod._stats_rows = {}
_rec_stats_mod.async_add_external_statistics = _default_add_external_statistics
_rec_stats_mod.statistics_during_period = _default_statistics_during_period


# --- pytest fixtures --------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture
def reset_stat_state():
    """Clear the captured calls between tests."""
    _rec_stats_mod._added_calls.clear()
    _rec_stats_mod._stats_rows = {}
    yield _rec_stats_mod
    _rec_stats_mod._added_calls.clear()
    _rec_stats_mod._stats_rows = {}


@pytest.fixture
def local_tz_utc():
    _dt_mod._LOCAL_TZ = timezone.utc
    yield
    _dt_mod._LOCAL_TZ = timezone.utc

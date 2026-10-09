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

import asyncio
import json
import logging
import sys
import types
from copy import deepcopy
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))


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


class CoreState(Enum):
    starting = 1
    running = 2
    stopping = 3


class Event:  # pragma: no cover - placeholder
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def callback(func):  # pragma: no cover - passthrough decorator
    return func


_core_mod.HomeAssistant = HomeAssistant
_core_mod.CoreState = CoreState
_core_mod.Event = Event
_core_mod.callback = callback


# ---- homeassistant.const ---------------------------------------------------
_const_mod = _ensure_module("homeassistant.const")


class UnitOfEnergy(str, Enum):
    KILO_WATT_HOUR = "kWh"


_const_mod.UnitOfEnergy = UnitOfEnergy
_const_mod.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"


# ---- homeassistant.util.dt -------------------------------------------------
_util_mod = _ensure_module("homeassistant.util")
_dt_mod = _ensure_module("homeassistant.util.dt")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def as_local(dt: datetime) -> datetime:
    # Tests pin a synthetic local tz via the `local_tz` fixture.
    tz = getattr(_dt_mod, "_LOCAL_TZ", UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)


_dt_mod.utcnow = utcnow
_dt_mod.as_utc = as_utc
_dt_mod.as_local = as_local
_dt_mod._LOCAL_TZ = UTC


# ---- homeassistant.helpers.event -------------------------------------------
_helpers_mod = _ensure_module("homeassistant.helpers")
_helpers_event_mod = _ensure_module("homeassistant.helpers.event")


def async_track_state_change_event(hass, entities, handler):  # pragma: no cover
    return lambda: None


_helpers_event_mod.async_track_state_change_event = async_track_state_change_event
_helpers_event_mod.async_track_time_interval = lambda *args: lambda: None

_helpers_start_mod = _ensure_module("homeassistant.helpers.start")


def async_at_started(hass, handler):
    if hass.state is CoreState.running:
        handler(hass)
        return lambda: None
    hass._started_callbacks.append(handler)
    return lambda: (
        hass._started_callbacks.remove(handler) if handler in hass._started_callbacks else None
    )


_helpers_start_mod.async_at_started = async_at_started
_entity_component_mod = _ensure_module("homeassistant.helpers.entity_component")
_entity_component_mod.DATA_INSTANCES = "entity_components"


def make_hass(*, running=False):
    hass = MagicMock()
    hass.state = CoreState.running if running else CoreState.starting
    hass.data = {}
    hass._started_callbacks = []
    hass.async_create_task = asyncio.create_task

    async def executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = executor
    return hass


# ---- homeassistant.helpers.storage -----------------------------------------
_helpers_storage_mod = _ensure_module("homeassistant.helpers.storage")
_TEST_STORAGE_DIR: Path | None = None


class Store:
    """File-backed stub with HA's log-and-return write failure behavior."""

    def __init__(self, hass, version, key, **kwargs) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        assert _TEST_STORAGE_DIR is not None
        self.path = str(_TEST_STORAGE_DIR / str(uuid4()))
        self.atomic_writes = kwargs.get("atomic_writes", False)

    @property
    def _data(self):
        path = Path(self.path)
        return json.loads(path.read_text())["data"] if path.exists() else None

    @_data.setter
    def _data(self, data):
        self._write_data(data)

    def _write_data(self, data):
        Path(self.path).write_text(
            json.dumps({"key": self.key, "version": self.version, "data": data})
        )

    async def async_load(self) -> dict[str, Any] | None:
        return deepcopy(self._data)

    async def async_save(self, data: dict[str, Any]) -> None:
        try:
            self._write_data(deepcopy(data))
        except (OSError, TypeError):
            logging.getLogger(__name__).error("Error writing config for %s", self.key)


_helpers_storage_mod.Store = Store


# ---- homeassistant.components.recorder -------------------------------------
_rec_root = _ensure_module("homeassistant.components")
_ensure_module("homeassistant.components.lg_thinq")
_lg_sensor_mod = _ensure_module("homeassistant.components.lg_thinq.sensor")


class ThinQEnergySensorEntity:
    pass


_lg_sensor_mod.ThinQEnergySensorEntity = ThinQEnergySensorEntity


def add_lg_entity(hass, entity_id="sensor.dryer_energy_yesterday"):
    """Expose the same entity -> coordinator -> SDK path as native LG."""
    entity = ThinQEnergySensorEntity()
    entity.entity_description = types.SimpleNamespace(key="yesterday", usage_period="DAILY")
    entity.property_id = "energyUsage"
    entity.coordinator = types.SimpleNamespace(
        api=types.SimpleNamespace(async_get_energy_usage=AsyncMock(return_value=[]))
    )
    component = MagicMock()
    component.get_entity.side_effect = lambda key: entity if key == entity_id else None
    hass.data["entity_components"] = {"sensor": component}
    return entity


_rec_mod = _ensure_module("homeassistant.components.recorder")
_rec_models_mod = _ensure_module("homeassistant.components.recorder.models")
_rec_stats_mod = _ensure_module("homeassistant.components.recorder.statistics")


class _RecorderInstance:
    async def async_add_executor_job(self, func, *args):
        return func(*args)

    async def async_block_till_done(self):
        pass


_REC_INSTANCE = _RecorderInstance()


def get_instance(hass):  # pragma: no cover
    return _REC_INSTANCE


_rec_mod.get_instance = get_instance


_rec_models_mod.StatisticData = dict
_rec_models_mod.StatisticMeanType = types.SimpleNamespace(NONE=0)


# These are replaced per-test by fixtures; defaults are inert.
def _default_add_external_statistics(hass, metadata, statistics):  # pragma: no cover
    statistics = list(statistics)
    _rec_stats_mod._added_calls.append((metadata, deepcopy(statistics)))
    rows = {
        row["start"]: row for row in _rec_stats_mod._stats_rows.get(metadata["statistic_id"], [])
    }
    for row in statistics:
        rows[row["start"].timestamp()] = {**row, "start": row["start"].timestamp()}
    _rec_stats_mod._stats_rows[metadata["statistic_id"]] = [rows[key] for key in sorted(rows)]


def _default_statistics_during_period(
    hass, start_time, end_time, statistic_ids, period, units, types
):  # pragma: no cover
    return {
        key: [
            deepcopy(row)
            for row in rows
            if row["start"] >= start_time.timestamp()
            and (end_time is None or row["start"] < end_time.timestamp())
        ]
        for key, rows in _rec_stats_mod._stats_rows.items()
        if key in statistic_ids
    }


_rec_stats_mod._added_calls = []
_rec_stats_mod._stats_rows = {}
_rec_stats_mod.async_add_external_statistics = _default_add_external_statistics
_rec_stats_mod.statistics_during_period = _default_statistics_during_period


# ---- homeassistant.components.recorder.history ----------------------------
_rec_history_mod = _ensure_module("homeassistant.components.recorder.history")


def _default_get_last_state_changes(hass, number_of_states, entity_id):
    """Default stub: return the configured rows keyed by entity_id."""
    return dict(_rec_history_mod._history_rows)


_rec_history_mod._history_rows = {}
_rec_history_mod.get_last_state_changes = _default_get_last_state_changes


# --- pytest fixtures --------------------------------------------------------
import pytest


@pytest.fixture(autouse=True)
def storage_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_TEST_STORAGE_DIR", tmp_path, raising=False)


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
    _dt_mod._LOCAL_TZ = UTC
    yield
    _dt_mod._LOCAL_TZ = UTC

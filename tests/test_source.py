"""Verify response dates and request throttling against the SDK response shape."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import add_lg_entity, make_hass
from lg_dryer_energy.source import YesterdaySource, daily_wh


@pytest.mark.parametrize("value", [0, 543, 1.5])
def test_daily_response_accepts_explicit_zero_and_wh(value):
    assert (
        daily_wh([{"usedDate": "20261008", "energyUsage": value}], date(2026, 10, 8), "energyUsage")
        == value
    )


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        {},
        [{"usedDate": "20261007", "energyUsage": 0}],
        [{"usedDate": "20261008"}],
        [{"usedDate": "20261008", "energyUsage": True}],
        [{"usedDate": "20261008", "energyUsage": "543"}],
        [{"usedDate": "20261008", "energyUsage": -1}],
        [{"usedDate": "20261008", "energyUsage": float("nan")}],
        [{"usedDate": "20261008", "energyUsage": float("inf")}],
        [{"usedDate": "20261008", "energyUsage": 1}] * 2,
    ],
)
def test_missing_ambiguous_and_invalid_daily_responses_are_not_zero(rows):
    with pytest.raises((ValueError, TypeError)):
        daily_wh(rows, date(2026, 10, 8), "energyUsage")


@pytest.mark.asyncio
async def test_uses_native_property_and_dates_without_mutating_entity():
    hass = make_hass(running=True)
    entity = add_lg_entity(hass)
    entity.property_id = "energyUsage_dryer"
    entity.coordinator.api.async_get_energy_usage.return_value = [
        {"usedDate": "20261008", "energyUsage_dryer": 543}
    ]
    source = YesterdaySource(hass, "sensor.dryer_energy_yesterday")
    day, now = date(2026, 10, 8), datetime(2026, 10, 9, tzinfo=UTC)
    assert await source.async_read(day, now, 0) == 543
    entity.coordinator.api.async_get_energy_usage.assert_awaited_once_with(
        energy_property="energyUsage_dryer",
        period="DAILY",
        start_date=day,
        end_date=day,
        detail=True,
    )


@pytest.mark.asyncio
async def test_equal_consecutive_days_require_separate_dated_reads():
    hass = make_hass(running=True)
    entity = add_lg_entity(hass)
    api = entity.coordinator.api.async_get_energy_usage
    api.side_effect = [
        [{"usedDate": "20261008", "energyUsage": 543}],
        [{"usedDate": "20261009", "energyUsage": 543}],
    ]
    source = YesterdaySource(hass, "sensor.dryer_energy_yesterday")
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert await source.async_read(date(2026, 10, 8), now, 543) == 543
    assert await source.async_read(date(2026, 10, 8), now + timedelta(hours=2), 543) == 543
    assert api.await_count == 1
    assert await source.async_read(date(2026, 10, 9), now + timedelta(days=1), 543) == 543
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_failure_and_correction_retries_are_limited_to_hourly():
    hass = make_hass(running=True)
    entity = add_lg_entity(hass)
    api = entity.coordinator.api.async_get_energy_usage
    api.side_effect = [
        RuntimeError("Offline"),
        [{"usedDate": "20261008", "energyUsage": 543}],
        [{"usedDate": "20261008", "energyUsage": 0}],
    ]
    source = YesterdaySource(hass, "sensor.dryer_energy_yesterday")
    day, now = date(2026, 10, 8), datetime(2026, 10, 9, tzinfo=UTC)
    assert await source.async_read(day, now, 543) is None
    assert await source.async_read(day, now + timedelta(minutes=59), 0) is None
    assert api.await_count == 1
    assert await source.async_read(day, now + timedelta(hours=1), 543) == 543
    assert await source.async_read(day, now + timedelta(minutes=61), 0) == 543
    assert api.await_count == 2
    assert await source.async_read(day, now + timedelta(hours=2), 0) == 0
    assert api.await_count == 3


@pytest.mark.asyncio
async def test_midnight_does_not_return_previous_dates_cached_value():
    hass = make_hass(running=True)
    entity = add_lg_entity(hass)
    api = entity.coordinator.api.async_get_energy_usage
    api.return_value = [{"usedDate": "20261008", "energyUsage": 543}]
    source = YesterdaySource(hass, "sensor.dryer_energy_yesterday")
    now = datetime(2026, 10, 9, 23, 59, tzinfo=UTC)
    assert await source.async_read(date(2026, 10, 8), now, 543) == 543
    assert await source.async_read(date(2026, 10, 9), now + timedelta(minutes=2), 543) is None
    assert api.await_count == 1


@pytest.mark.asyncio
async def test_template_entity_cannot_supply_authoritative_daily_energy():
    hass = make_hass(running=True)
    entity = add_lg_entity(hass)
    component = hass.data["entity_components"]["sensor"]
    component.get_entity.side_effect = None
    component.get_entity.return_value = SimpleNamespace()
    source = YesterdaySource(hass, "sensor.dryer_energy_yesterday")
    assert (
        await source.async_read(date(2026, 10, 8), datetime(2026, 10, 9, tzinfo=UTC), 543) is None
    )
    entity.coordinator.api.async_get_energy_usage.assert_not_awaited()

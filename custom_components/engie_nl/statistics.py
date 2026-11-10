"""Mijn ENGIE meter readings → Home Assistant long-term statistics.

ENGIE reads the smart meter about once a month and lists only the last couple of
readings, so the integration keeps its own reading history (a Store per entry)
and rebuilds the external statistics from it on every refresh. Each reading is a
cumulative step at local midnight of its date, which the Energy dashboard shows
as one bar per reading period (about a month).

Cost statistics use the *current* all-in tariff (incl. VAT and energy tax) for
every period, so months before a tariff change are approximate. Fixed charges,
network costs and the tax credit are per day and not included.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, cast

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import EngieData
from .const import DOMAIN, STORAGE_KEY_READINGS, STORAGE_VERSION
from .parse import (
    REGISTER_TARIFF,
    covered_through,
    cumulative_points,
    daily_costs,
    daily_prices,
    daily_usage,
    merge_month_totals,
    merge_readings,
    running_sum,
)

_LOGGER = logging.getLogger(__name__)

LABELS = {
    "electricity_low": "electricity low tariff",
    "electricity_normal": "electricity normal tariff",
    "electricity_single": "electricity",
    "return_low": "electricity returned low tariff",
    "return_normal": "electricity returned normal tariff",
    "return_single": "electricity returned",
    "gas": "gas",
}


def statistic_id(key: str, cost: bool = False) -> str:
    """e.g. ``engie_nl:electricity_low`` / ``engie_nl:electricity_low_cost``."""
    return f"{DOMAIN}:{key}{'_cost' if cost else ''}"


def _metadata(stat_id: str, name: str, unit: str, unit_class: Optional[str]) -> StatisticMetaData:
    """Sum-only metadata that works across HA versions (mean_type / unit_class)."""
    metadata: dict[str, Any] = {
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_of_measurement": unit,
    }
    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        metadata["mean_type"] = StatisticMeanType.NONE
    except ImportError:
        metadata["has_mean"] = False
    if "unit_class" in StatisticMetaData.__annotations__:
        metadata["unit_class"] = unit_class
    return cast(StatisticMetaData, metadata)


class ReadingHistory:
    """Keeps every meter reading seen, per register."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_READINGS}.{entry_id}"
        )
        self.history: dict[str, list[list[Any]]] = {}
        # {electricity|return|gas: [[iso day covered, "YYYY-MM", month total], ...]}
        self.month_totals: dict[str, list[list[Any]]] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self.history = stored.get("readings", {})
        self.month_totals = stored.get("month_totals", {})

    async def async_merge(self, data: EngieData) -> None:
        merged = merge_readings(self.history, data.readings)
        covered = covered_through(data.metering_points, dt_util.now().date())
        totals = merge_month_totals(self.month_totals, data.consumption, covered)
        if merged != self.history or totals != self.month_totals:
            self.history = merged
            self.month_totals = totals
            await self._store.async_save({"readings": merged, "month_totals": totals})

    def daily(self, data: EngieData) -> dict[str, Any]:
        """Per-day usage derived from the month totals, plus the total cost per day."""
        usage = {key: daily_usage(items) for key, items in self.month_totals.items()}
        return {"usage": usage, "cost": daily_costs(usage, daily_prices(data.metering_points))}

    async def async_remove(self) -> None:
        await self._store.async_remove()


def async_update_statistics(hass: HomeAssistant, history: dict[str, list[list[Any]]], data: EngieData) -> None:
    """(Re)import usage and cost statistics for every register."""
    for key, readings in history.items():
        if not readings:
            continue
        kind, tariff_key = REGISTER_TARIFF.get(key, ("ELK", ""))
        point = data.point(kind)
        price = point.tariffs.get(tariff_key) if point else None
        is_gas = key == "gas"
        unit = UnitOfVolume.CUBIC_METERS if is_gas else UnitOfEnergy.KILO_WATT_HOUR
        points = cumulative_points(readings, price)

        def start(day: Any) -> datetime:
            return dt_util.start_of_local_day(day)

        try:
            async_add_external_statistics(
                hass,
                _metadata(statistic_id(key), f"ENGIE {LABELS.get(key, key)}", unit, "volume" if is_gas else "energy"),
                [StatisticData(start=start(d), state=value, sum=total) for d, value, total, _ in points],
            )
            if price is not None:
                async_add_external_statistics(
                    hass,
                    _metadata(statistic_id(key, cost=True), f"ENGIE {LABELS.get(key, key)} cost", "EUR", None),
                    [StatisticData(start=start(d), state=cost, sum=cost) for d, _, _, cost in points],
                )
        except Exception:  # noqa: BLE001 - recorder may be unavailable; try again next refresh
            _LOGGER.debug("Could not import ENGIE statistics for %s", key, exc_info=True)


DAILY_LABELS = {"electricity": "electricity per day", "return": "electricity returned per day", "gas": "gas per day"}


def async_update_daily_statistics(hass: HomeAssistant, daily: dict[str, Any]) -> None:
    """Import per-day usage (and total cost) as statistics dated on the day itself.

    A plain sensor would book yesterday's usage on today (when it is fetched);
    external statistics put it at local midnight of the day it belongs to.
    Ids: ``engie_nl:electricity_daily``, ``return_daily``, ``gas_daily``, ``cost_daily``.
    """

    def rows(days: list[tuple[Any, float]]) -> list[StatisticData]:
        return [
            StatisticData(start=dt_util.start_of_local_day(day), state=value, sum=total)
            for day, value, total in running_sum(days)
        ]

    try:
        for key, days in daily["usage"].items():
            if not days:
                continue
            is_gas = key == "gas"
            async_add_external_statistics(
                hass,
                _metadata(
                    statistic_id(f"{key}_daily"),
                    f"ENGIE {DAILY_LABELS.get(key, key)}",
                    UnitOfVolume.CUBIC_METERS if is_gas else UnitOfEnergy.KILO_WATT_HOUR,
                    "volume" if is_gas else "energy",
                ),
                rows([(day, value) for day, value, _ in days]),
            )
        if daily["cost"]:
            async_add_external_statistics(
                hass, _metadata(statistic_id("cost_daily"), "ENGIE cost per day", "EUR", None), rows(daily["cost"])
            )
    except Exception:  # noqa: BLE001 - recorder may be unavailable; try again next refresh
        _LOGGER.debug("Could not import ENGIE daily statistics", exc_info=True)

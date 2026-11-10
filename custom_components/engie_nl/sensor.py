"""Sensors for Mijn ENGIE: rates, contract, meter readings, usage and bills."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import EngieData
from .const import ATTRIBUTION, DOMAIN, NAME
from .coordinator import EngieConfigEntry, EngieCoordinator

EUR = "EUR"
EUR_KWH = "EUR/kWh"
EUR_M3 = "EUR/m³"
EUR_DAY = "EUR/d"


def _tariff(kind: str, key: str) -> Callable[[EngieData, EngieCoordinator], Any]:
    def value(data: EngieData, _: EngieCoordinator) -> Any:
        point = data.point(kind)
        return point.tariffs.get(key) if point else None

    return value


def _reading(key: str) -> Callable[[EngieData, EngieCoordinator], Any]:
    def value(_: EngieData, coordinator: EngieCoordinator) -> Any:
        readings = coordinator.history.history.get(key) or []
        return readings[-1][1] if readings else None

    return value


def _reading_attrs(key: str) -> Callable[[EngieData, EngieCoordinator], dict[str, Any]]:
    def attrs(_: EngieData, coordinator: EngieCoordinator) -> dict[str, Any]:
        readings = coordinator.history.history.get(key) or []
        return {"reading_date": readings[-1][0], "readings_stored": len(readings)} if readings else {}

    return attrs


def _dict(attr: str, key: str) -> Callable[[EngieData, EngieCoordinator], Any]:
    def value(data: EngieData, _: EngieCoordinator) -> Any:
        source = getattr(data, attr) or {}
        return source.get(key) if isinstance(source, dict) else None

    return value


def _yearly(kind: str, keys: tuple[str, ...]) -> Callable[[EngieData, EngieCoordinator], Any]:
    def value(data: EngieData, _: EngieCoordinator) -> Any:
        point = data.point(kind)
        if not point:
            return None
        values = [point.yearly_estimate.get(k) for k in keys]
        return sum(v for v in values if v) if any(v is not None for v in values) else None

    return value


def _last_day(coordinator: EngieCoordinator, key: str) -> tuple[Any, ...] | None:
    """Latest completed day for a daily key ("cost" or a usage key), unless it is stale.

    Normally that is yesterday. If ENGIE is a day behind it is the day before; older
    than that the sensor shows unknown instead of an outdated number.
    """
    days = coordinator.daily["cost"] if key == "cost" else coordinator.daily["usage"].get(key) or []
    if not days or days[-1][0] < dt_util.now().date() - timedelta(days=2):
        return None
    return days[-1]


def _daily(key: str) -> Callable[[EngieData, EngieCoordinator], Any]:
    def value(_: EngieData, coordinator: EngieCoordinator) -> Any:
        last = _last_day(coordinator, key)
        return last[1] if last else None

    return value


def _daily_attrs(key: str) -> Callable[[EngieData, EngieCoordinator], dict[str, Any]]:
    def attrs(_: EngieData, coordinator: EngieCoordinator) -> dict[str, Any]:
        last = _last_day(coordinator, key)
        if not last:
            return {}
        result: dict[str, Any] = {"date": last[0].isoformat()}
        if len(last) > 2:
            # > 1 means ENGIE skipped days and the amount was divided evenly over them
            result["days_averaged"] = last[2]
        return result

    return attrs


def _contract_end(data: EngieData, _: EngieCoordinator) -> date | None:
    ends = [p.contract_end for p in data.metering_points if p.contract_end]
    return min(ends) if ends else None


def _next_meter_request(data: EngieData, _: EngieCoordinator) -> date | None:
    days = [p.next_meter_request_date for p in data.metering_points if p.next_meter_request_date]
    return min(days) if days else None


def _product(data: EngieData, _: EngieCoordinator) -> str | None:
    point = data.point("ELK") or (data.metering_points[0] if data.metering_points else None)
    return point.product_name if point else None


def _product_attrs(data: EngieData, _: EngieCoordinator) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for point in data.metering_points:
        label = "electricity" if point.kind == "ELK" else "gas" if point.kind == "GAS" else point.kind.lower()
        attrs[f"{label}_product"] = point.product_name
        attrs[f"{label}_product_type"] = point.product_type
        attrs[f"{label}_contract_start"] = point.contract_start.isoformat() if point.contract_start else None
        attrs[f"{label}_contract_end"] = point.contract_end.isoformat() if point.contract_end else None
        attrs[f"{label}_grid_operator"] = point.grid_operator
    attrs["fixed_contract"] = data.flags.get("hasFixedContract")
    attrs["dynamic"] = data.flags.get("hasDynamicProduct")
    return attrs


def _prepayment(data: EngieData, _: EngieCoordinator) -> float | None:
    values = [p.prepayment_amount for p in data.metering_points if p.prepayment_amount is not None]
    return round(sum(values), 2) if values else None


def _latest_invoice(data: EngieData) -> dict[str, Any] | None:
    items = [t for t in data.transactions if isinstance(t, dict) and t.get("date")]
    return max(items, key=lambda t: str(t.get("date"))) if items else None


def _latest_invoice_amount(data: EngieData, _: EngieCoordinator) -> Any:
    item = _latest_invoice(data)
    return item.get("amount") if item else None


def _latest_invoice_attrs(data: EngieData, _: EngieCoordinator) -> dict[str, Any]:
    item = _latest_invoice(data)
    if not item:
        return {}
    keys = ("date", "due_date", "type", "description", "status", "status_description", "balance", "payment_possible")
    return {k: item.get(k) for k in keys} | {"transactions": len(data.transactions)}


@dataclass(frozen=True, kw_only=True)
class EngieSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EngieData, EngieCoordinator], Any]
    attrs_fn: Callable[[EngieData, EngieCoordinator], dict[str, Any]] | None = None
    needs: str | None = None  # "ELK" / "GAS": only created when that connection exists


def _money(key: str, name: str, value_fn: Callable, icon: str = "mdi:cash", **kw: Any) -> EngieSensorDescription:
    return EngieSensorDescription(
        key=key, name=name, value_fn=value_fn, icon=icon,
        device_class=SensorDeviceClass.MONETARY, native_unit_of_measurement=EUR, suggested_display_precision=2, **kw,
    )


SENSORS: tuple[EngieSensorDescription, ...] = (
    # Rates (all-in = supply + energy tax + renewable surcharge, incl. VAT)
    EngieSensorDescription(key="electricity_tariff_normal", name="Electricity price normal", needs="ELK",
        value_fn=_tariff("ELK", "tariff_normal"), native_unit_of_measurement=EUR_KWH, icon="mdi:flash", suggested_display_precision=4),
    EngieSensorDescription(key="electricity_tariff_low", name="Electricity price low", needs="ELK",
        value_fn=_tariff("ELK", "tariff_low"), native_unit_of_measurement=EUR_KWH, icon="mdi:flash-outline", suggested_display_precision=4),
    EngieSensorDescription(key="electricity_return_tariff_normal", name="Feed-in price normal", needs="ELK",
        value_fn=_tariff("ELK", "return_tariff_normal"), native_unit_of_measurement=EUR_KWH, icon="mdi:transmission-tower-export", suggested_display_precision=4),
    EngieSensorDescription(key="electricity_return_tariff_low", name="Feed-in price low", needs="ELK",
        value_fn=_tariff("ELK", "return_tariff_low"), native_unit_of_measurement=EUR_KWH, icon="mdi:transmission-tower-export", suggested_display_precision=4),
    EngieSensorDescription(key="electricity_fixed_charge", name="Electricity fixed charge", needs="ELK",
        value_fn=_tariff("ELK", "fixed_charge"), native_unit_of_measurement=EUR_DAY, icon="mdi:calendar-today", suggested_display_precision=4),
    EngieSensorDescription(key="electricity_operator_fee", name="Electricity network costs", needs="ELK",
        value_fn=_tariff("ELK", "operator_fee"), native_unit_of_measurement=EUR_DAY, icon="mdi:transmission-tower", suggested_display_precision=4),
    EngieSensorDescription(key="electricity_tax_credit", name="Energy tax credit", needs="ELK",
        value_fn=_tariff("ELK", "tax_credit"), native_unit_of_measurement=EUR_DAY, icon="mdi:cash-refund", suggested_display_precision=4),
    EngieSensorDescription(key="gas_tariff", name="Gas price", needs="GAS",
        value_fn=_tariff("GAS", "tariff_single"), native_unit_of_measurement=EUR_M3, icon="mdi:fire", suggested_display_precision=4),
    EngieSensorDescription(key="gas_fixed_charge", name="Gas fixed charge", needs="GAS",
        value_fn=_tariff("GAS", "fixed_charge"), native_unit_of_measurement=EUR_DAY, icon="mdi:calendar-today", suggested_display_precision=4),
    EngieSensorDescription(key="gas_operator_fee", name="Gas network costs", needs="GAS",
        value_fn=_tariff("GAS", "operator_fee"), native_unit_of_measurement=EUR_DAY, icon="mdi:pipe", suggested_display_precision=4),
    # Contract
    EngieSensorDescription(key="product", name="Product", value_fn=_product, attrs_fn=_product_attrs, icon="mdi:file-document-outline"),
    EngieSensorDescription(key="contract_end", name="Contract end", value_fn=_contract_end, device_class=SensorDeviceClass.DATE, icon="mdi:calendar-end"),
    _money("prepayment", "Monthly advance payment", _prepayment, icon="mdi:bank-transfer"),
    EngieSensorDescription(key="next_meter_request", name="Next meter reading request", value_fn=_next_meter_request,
        device_class=SensorDeviceClass.DATE, entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:counter"),
    EngieSensorDescription(key="electricity_yearly_estimate", name="Electricity yearly estimate", needs="ELK",
        value_fn=_yearly("ELK", ("normal", "low", "single")), device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, entity_category=EntityCategory.DIAGNOSTIC),
    EngieSensorDescription(key="gas_yearly_estimate", name="Gas yearly estimate", needs="GAS",
        value_fn=_yearly("GAS", ("single", "normal", "low")), device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS, entity_category=EntityCategory.DIAGNOSTIC),
    # Meter readings (latest, about monthly)
    *(
        EngieSensorDescription(key=f"{key}_reading", name=name, needs=kind, value_fn=_reading(key), attrs_fn=_reading_attrs(key),
            device_class=SensorDeviceClass.GAS if kind == "GAS" else SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfVolume.CUBIC_METERS if kind == "GAS" else UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING, suggested_display_precision=0)
        for key, name, kind in (
            ("electricity_low", "Meter reading electricity low", "ELK"),
            ("electricity_normal", "Meter reading electricity normal", "ELK"),
            ("return_low", "Meter reading returned low", "ELK"),
            ("return_normal", "Meter reading returned normal", "ELK"),
            ("gas", "Meter reading gas", "GAS"),
        )
    ),
    # This month's usage
    EngieSensorDescription(key="consumption_power_month", name="Electricity used this month", needs="ELK",
        value_fn=_dict("consumption", "totalConsumptionPower"), device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=0),
    EngieSensorDescription(key="redelivery_power_month", name="Electricity returned this month", needs="ELK",
        value_fn=_dict("consumption", "totalRedeliveryPower"), device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=0),
    EngieSensorDescription(key="consumption_gas_month", name="Gas used this month", needs="GAS",
        value_fn=_dict("consumption", "totalConsumptionGas"), device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS, suggested_display_precision=0),
    # Last completed day (derived from the daily growth of the month total)
    EngieSensorDescription(key="consumption_power_yesterday", name="Electricity used yesterday", needs="ELK",
        value_fn=_daily("electricity"), attrs_fn=_daily_attrs("electricity"), device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=1, icon="mdi:flash"),
    EngieSensorDescription(key="consumption_gas_yesterday", name="Gas used yesterday", needs="GAS",
        value_fn=_daily("gas"), attrs_fn=_daily_attrs("gas"), device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS, suggested_display_precision=1, icon="mdi:fire"),
    _money("cost_yesterday", "Energy cost yesterday", _daily("cost"), attrs_fn=_daily_attrs("cost"), icon="mdi:cash-clock"),
    # Annual statement forecast and bills
    _money("expected_annual_statement", "Expected annual statement", _dict("estimations", "total_estimated_nota_amount"), icon="mdi:file-chart"),
    _money("expected_settlement", "Expected settlement", _dict("estimations", "to_pay_balance_amount"), icon="mdi:scale-balance"),
    _money("advised_prepayment", "Advised advance payment", _dict("estimations", "prepayment_amount_advice"), icon="mdi:lightbulb-on-outline"),
    EngieSensorDescription(key="prepayments_left", name="Advance payments left", value_fn=_dict("estimations", "number_of_prepayments_left"),
        icon="mdi:counter", entity_category=EntityCategory.DIAGNOSTIC),
    _money("open_amount", "Open amount", _dict("payments_info", "current_debt_amount"), icon="mdi:cash-clock"),
    _money("latest_invoice", "Latest invoice", _latest_invoice_amount, attrs_fn=_latest_invoice_attrs, icon="mdi:receipt-text"),
)


async def async_setup_entry(hass: HomeAssistant, entry: EngieConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    kinds = {p.kind for p in coordinator.data.metering_points}
    async_add_entities(EngieSensor(coordinator, d) for d in SENSORS if d.needs is None or d.needs in kinds)


class EngieSensor(CoordinatorEntity[EngieCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    entity_description: EngieSensorDescription

    def __init__(self, coordinator: EngieCoordinator, description: EngieSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=NAME,
            manufacturer="ENGIE Nederland",
            model="Mijn ENGIE (unofficial)",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://mijn.engie.nl/",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, self.coordinator)

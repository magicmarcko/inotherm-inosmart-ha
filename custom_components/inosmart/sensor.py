"""Sensor platform for Inotherm Inosmart BLE."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import InosmartDoorManager, InosmartSnapshot
from .entity import InosmartEntity


@dataclass(frozen=True, kw_only=True)
class InosmartSensorDescription(SensorEntityDescription):
    value_fn: Callable[[InosmartDoorManager, InosmartSnapshot], Any]


SENSORS: tuple[InosmartSensorDescription, ...] = (
    InosmartSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _m, s: s.device_info.main_fw_version if s.device_info else None,
    ),
    InosmartSensorDescription(
        key="main_lock_counter",
        translation_key="main_lock_counter",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _m, s: s.system_info.mainlock_counter if s.system_info else None,
    ),
    InosmartSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _m, s: s.sys_clock.temperature_c if s.sys_clock else None,
    ),
    InosmartSensorDescription(
        key="client_imei",
        translation_key="client_imei",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m, _s: m.imei,
    ),
    InosmartSensorDescription(
        key="last_status",
        translation_key="last_status",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _m, s: s.last_task_status,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: InosmartDoorManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([InosmartSensor(manager, desc) for desc in SENSORS])


class InosmartSensor(InosmartEntity, SensorEntity):
    entity_description: InosmartSensorDescription

    def __init__(
        self, manager: InosmartDoorManager, description: InosmartSensorDescription
    ) -> None:
        super().__init__(manager, f"sensor_{description.key}")
        self.entity_description = description

    @property
    def native_value(self) -> Optional[Any]:
        return self.entity_description.value_fn(self._manager, self._manager.snapshot)

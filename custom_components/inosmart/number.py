"""Number platform for Inotherm Inosmart LED brightness settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import InosmartDoorManager
from .entity import InosmartEntity
from .protocol import MAX_EXTERNAL_LED_POWER, MAX_INTEGRATED_LED_POWER


@dataclass(frozen=True, kw_only=True)
class InosmartNumberDescription(NumberEntityDescription):
    attr_name: str
    is_hled: bool


NUMBERS: tuple[InosmartNumberDescription, ...] = (
    InosmartNumberDescription(
        key="integrated_led",
        translation_key="integrated_led",
        icon="mdi:led-strip-variant",
        native_min_value=0,
        native_max_value=MAX_INTEGRATED_LED_POWER,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        attr_name="hled",
        is_hled=True,
    ),
    InosmartNumberDescription(
        key="external_led",
        translation_key="external_led",
        icon="mdi:lightbulb-on-outline",
        native_min_value=0,
        native_max_value=MAX_EXTERNAL_LED_POWER,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        attr_name="sled",
        is_hled=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: InosmartDoorManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([InosmartLedNumber(manager, desc) for desc in NUMBERS])


class InosmartLedNumber(InosmartEntity, NumberEntity):
    """Represents HLED / SLED brightness (requires Admin Password + Button 3 / Demo Mode)."""

    entity_description: InosmartNumberDescription

    def __init__(
        self, manager: InosmartDoorManager, description: InosmartNumberDescription
    ) -> None:
        super().__init__(manager, f"num_{description.key}")
        self.entity_description = description

    @property
    def native_value(self) -> Optional[float]:
        sys_info = self._manager.snapshot.system_info
        if sys_info is None:
            return None
        return float(getattr(sys_info, self.entity_description.attr_name))

    async def async_set_native_value(self, value: float) -> None:
        ival = int(round(value))
        if self.entity_description.is_hled:
            await self._manager.async_set_leds(hled=ival)
        else:
            await self._manager.async_set_leds(sled=ival)

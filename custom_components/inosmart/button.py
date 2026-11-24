"""Button platform for Inotherm Inosmart BLE."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Coroutine, Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import InosmartDoorManager
from .entity import InosmartEntity
from .protocol import BleRTask


@dataclass(frozen=True, kw_only=True)
class InosmartButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[InosmartDoorManager], Coroutine[Any, Any, Any]]


BUTTONS: tuple[InosmartButtonDescription, ...] = (
    InosmartButtonDescription(
        key="open_door",
        translation_key="open_door",
        icon="mdi:door-open",
        press_fn=lambda m: m.async_unlock(BleRTask.MAIN_KEYLOCK_OPEN),
    ),
    InosmartButtonDescription(
        key="ready_to_open",
        translation_key="ready_to_open",
        icon="mdi:door-sliding-open",
        press_fn=lambda m: m.async_unlock(BleRTask.MAIN_KEYLOCK_READY2OPEN),
    ),
    InosmartButtonDescription(
        key="relay2_open",
        translation_key="relay2_open",
        icon="mdi:electric-switch",
        press_fn=lambda m: m.async_unlock(BleRTask.RELAY2_OPEN),
    ),
    InosmartButtonDescription(
        key="gw_relay1_open",
        translation_key="gw_relay1_open",
        icon="mdi:gate",
        entity_registry_enabled_default=False,
        press_fn=lambda m: m.async_unlock(BleRTask.GW_RELAY1_OPEN),
    ),
    InosmartButtonDescription(
        key="gw_relay2_open",
        translation_key="gw_relay2_open",
        icon="mdi:gate-open",
        entity_registry_enabled_default=False,
        press_fn=lambda m: m.async_unlock(BleRTask.GW_RELAY2_OPEN),
    ),
    InosmartButtonDescription(
        key="pair_phone",
        translation_key="pair_phone",
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda m: m.async_pair(),
    ),
    InosmartButtonDescription(
        key="refresh_info",
        translation_key="refresh_info",
        icon="mdi:refresh",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda m: m.async_refresh_info(include_admin=True),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: InosmartDoorManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([InosmartButton(manager, desc) for desc in BUTTONS])


class InosmartButton(InosmartEntity, ButtonEntity):
    entity_description: InosmartButtonDescription

    def __init__(
        self, manager: InosmartDoorManager, description: InosmartButtonDescription
    ) -> None:
        super().__init__(manager, f"btn_{description.key}")
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self._manager)

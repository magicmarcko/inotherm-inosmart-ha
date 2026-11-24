"""Config flow for Inotherm Inosmart BLE."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from bleak import BleakClient
from bleak_retry_connector import establish_connection
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .client import InosmartBleClient, InosmartError, InosmartNotPairingModeError
from .const import (
    CONF_ADMIN_PASSWORD,
    CONF_DOOR_NAME,
    CONF_IMEI,
    CONF_PAIR_ON_SETUP,
    DOMAIN,
)
from .protocol import (
    UUID_ADVERTISING_SERVICE,
    UUID_SERVICE,
    generate_synthetic_imei,
    validate_imei,
)

_LOGGER = logging.getLogger(__name__)


def _is_inosmart_adv(service_info: BluetoothServiceInfoBleak) -> bool:
    uuids = {u.lower() for u in service_info.service_uuids}
    if UUID_ADVERTISING_SERVICE in uuids or UUID_SERVICE in uuids:
        return True
    return (service_info.name or "").upper().startswith(("INO", "IN0"))


class InosmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Inotherm Inosmart BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: Optional[BluetoothServiceInfoBleak] = None
        self._address: str = ""
        self._door_name: str = "Haustür Inosmart"
        self._imei: str = ""
        self._admin_password: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return InosmartOptionsFlowHandler(config_entry)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle discovery via Home Assistant Bluetooth / ESPHome Proxy."""
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self._address = discovery_info.address.upper()
        self._door_name = discovery_info.name or "Haustür Inosmart"
        self.context["title_placeholders"] = {"name": self._door_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Confirm Bluetooth discovery & optional pairing."""
        if user_input is not None:
            self._door_name = user_input[CONF_DOOR_NAME]
            self._admin_password = user_input.get(CONF_ADMIN_PASSWORD, "")
            self._imei = user_input[CONF_IMEI].strip()
            try:
                self._imei = validate_imei(self._imei)
            except ValueError:
                return self._show_confirm_form(errors={CONF_IMEI: "invalid_imei"})
            if user_input.get(CONF_PAIR_ON_SETUP, True):
                return await self._try_pair_and_create()
            return self._create_entry()

        self._imei = self._imei or generate_synthetic_imei()
        return self._show_confirm_form()

    def _show_confirm_form(self, errors: Optional[Dict[str, str]] = None) -> FlowResult:
        schema = vol.Schema(
            {
                vol.Required(CONF_DOOR_NAME, default=self._door_name): str,
                vol.Required(CONF_IMEI, default=self._imei): str,
                vol.Optional(CONF_ADMIN_PASSWORD, default=self._admin_password): str,
                vol.Optional(CONF_PAIR_ON_SETUP, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={"address": self._address, "imei": self._imei},
        )

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle manual or discovered selection."""
        errors: Dict[str, str] = {}
        discovered: Dict[str, str] = {}
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if _is_inosmart_adv(info):
                discovered[info.address.upper()] = (
                    f"{info.name or 'Inosmart'} ({info.address.upper()}, RSSI {info.rssi})"
                )

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            try:
                imei = validate_imei(user_input[CONF_IMEI])
            except ValueError:
                errors[CONF_IMEI] = "invalid_imei"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._address = address
                self._door_name = user_input[CONF_DOOR_NAME]
                self._imei = imei
                self._admin_password = user_input.get(CONF_ADMIN_PASSWORD, "")
                if user_input.get(CONF_PAIR_ON_SETUP, True):
                    return await self._try_pair_and_create(step_id="user")
                return self._create_entry()

        default_addr = next(iter(discovered), "")
        self._imei = self._imei or generate_synthetic_imei()
        if discovered:
            addr_field: Any = vol.In(discovered)
        else:
            addr_field = str

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default=default_addr): addr_field,
                vol.Required(CONF_DOOR_NAME, default=self._door_name): str,
                vol.Required(CONF_IMEI, default=self._imei): str,
                vol.Optional(CONF_ADMIN_PASSWORD, default=""): str,
                vol.Optional(CONF_PAIR_ON_SETUP, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"imei": self._imei},
        )

    def _build_retry_schema(self, step_id: str) -> vol.Schema:
        """Build form schema for pairing retries, preserving address on the user step."""
        fields: Dict[Any, Any] = {}
        if step_id == "user":
            fields[vol.Required(CONF_ADDRESS, default=self._address)] = str
        fields.update(
            {
                vol.Required(CONF_DOOR_NAME, default=self._door_name): str,
                vol.Required(CONF_IMEI, default=self._imei): str,
                vol.Optional(CONF_ADMIN_PASSWORD, default=self._admin_password): str,
                vol.Optional(CONF_PAIR_ON_SETUP, default=True): bool,
            }
        )
        return vol.Schema(fields)

    async def _try_pair_and_create(self, step_id: str = "confirm") -> FlowResult:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            return self.async_show_form(
                step_id=step_id,
                data_schema=self._build_retry_schema(step_id),
                errors={"base": "cannot_connect"},
            )

        async def _connect(dev: Any) -> BleakClient:
            return await establish_connection(BleakClient, dev, self._door_name, max_attempts=2)

        client = InosmartBleClient(ble_device, self._imei, connector=_connect)
        try:
            await client.connect()
            await client.pair_phone()
        except InosmartNotPairingModeError:
            err_key = "not_in_pairing_mode"
        except InosmartError as err:
            _LOGGER.warning("BLE pairing error: %s", err)
            err_key = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected pairing error")
            err_key = "unknown"
        else:
            await client.disconnect()
            return self._create_entry()

        await client.disconnect()
        return self.async_show_form(
            step_id=step_id,
            data_schema=self._build_retry_schema(step_id),
            errors={"base": err_key},
        )

    def _create_entry(self) -> FlowResult:
        return self.async_create_entry(
            title=self._door_name,
            data={
                CONF_ADDRESS: self._address,
                CONF_DOOR_NAME: self._door_name,
                CONF_IMEI: self._imei,
                CONF_ADMIN_PASSWORD: self._admin_password,
            },
        )


class InosmartOptionsFlowHandler(config_entries.OptionsFlow):
    """Allow updating optional admin password or door name."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            new_data = dict(self._entry.data)
            new_data[CONF_DOOR_NAME] = user_input[CONF_DOOR_NAME]
            new_data[CONF_ADMIN_PASSWORD] = user_input.get(CONF_ADMIN_PASSWORD, "")
            self.hass.config_entries.async_update_entry(
                self._entry, title=user_input[CONF_DOOR_NAME], data=new_data
            )
            return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DOOR_NAME,
                    default=self._entry.data.get(CONF_DOOR_NAME, self._entry.title),
                ): str,
                vol.Optional(
                    CONF_ADMIN_PASSWORD,
                    default=self._entry.data.get(CONF_ADMIN_PASSWORD, ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"imei": self._entry.data.get(CONF_IMEI, "")},
        )

"""Config flow for Surplife BLE with discovery, validation, and manual fallback."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from bleak import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfo, BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Service data UUID prefix advertised by IOTBT/Surplife BLE lights.
# Devices may use 00005a00, 00005a02, etc. — match by prefix.
_SURPLIFE_SERVICE_DATA_PREFIX = "00005a"

# Timeout for connection validation during config flow
_VALIDATE_TIMEOUT = 10


def _matches_surplife(service_info: BluetoothServiceInfo) -> bool:
    """Check whether an advertised device looks like a Surplife BLE light."""
    local_name = (service_info.name or "").upper()
    if local_name.startswith("IOTBT"):
        return True
    return any(
        uuid.startswith(_SURPLIFE_SERVICE_DATA_PREFIX)
        for uuid in service_info.service_data
    )


async def _validate_device(hass: HomeAssistant, address: str) -> str | None:
    """Try to connect and verify the device responds.

    Returns an error key on failure, None on success.
    """
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

    ble_device = bluetooth.async_ble_device_from_address(hass, address, True)
    if ble_device is None:
        return "device_not_found"

    try:
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            address,
            lambda _c: None,
            use_services_cache=True,
            ble_device_callback=lambda: (
                bluetooth.async_ble_device_from_address(hass, address, True) or ble_device
            ),
            timeout=_VALIDATE_TIMEOUT,
        )
    except TimeoutError:
        return "cannot_connect"
    except BleakError:
        return "cannot_connect"
    else:
        await client.disconnect()
        return None


class SurplifeBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual setup for Surplife BLE lights."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle discovery via bluetooth advertisement matching."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self._discovered_devices[discovery_info.address] = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address,
        }
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show discovered devices or accept manual address entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            name = user_input.get(CONF_NAME, "").strip() or "Surplife BLE light"

            if address == "__manual":
                # Show manual entry form
                return await self.async_step_manual()

            await self.async_set_unique_id(address.upper())
            self._abort_if_unique_id_configured()

            # Validate connection before creating entry
            error = await _validate_device(self.hass, address)
            if error:
                errors[CONF_ADDRESS] = error
            else:
                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address},
                )

        # Gather discovered devices that match our criteria
        if self._discovery_info is not None:
            self._discovered_devices[self._discovery_info.address] = self._discovery_info

        for service_info in bluetooth.async_discovered_service_info(self.hass):
            if service_info.address in self._discovered_devices:
                continue
            if _matches_surplife(service_info):
                self._discovered_devices[service_info.address] = service_info

        # Build dropdown of discovered devices + manual entry option
        discovered_options = {
            addr: info.name or addr
            for addr, info in self._discovered_devices.items()
        }

        if discovered_options:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {"__manual": "Enter address manually", **discovered_options}
                    ),
                    vol.Optional(CONF_NAME, default=""): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_NAME, default="Surplife BLE light"): str,
                }
            )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "discovered_count": str(len(discovered_options)),
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual address entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            name = user_input.get(CONF_NAME, "").strip() or "Surplife BLE light"
            await self.async_set_unique_id(address.upper())
            self._abort_if_unique_id_configured()

            error = await _validate_device(self.hass, address)
            if error:
                errors[CONF_ADDRESS] = error
            else:
                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_NAME, default="Surplife BLE light"): str,
                }
            ),
            errors=errors,
            description_placeholders={},
        )

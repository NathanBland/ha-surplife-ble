"""Set up the Surplife BLE integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Surplife BLE device from a config entry."""
    from homeassistant.components import bluetooth
    from homeassistant.const import CONF_ADDRESS, Platform
    from homeassistant.core import callback

    from .const import CONF_DISCONNECT_DELAY, DEFAULT_DISCONNECT_DELAY
    from .coordinator import SurplifeCoordinator, SurplifeRuntimeData

    address = entry.data[CONF_ADDRESS]
    coordinator = SurplifeCoordinator(hass, entry)
    entry.runtime_data = SurplifeRuntimeData(coordinator=coordinator)

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        coordinator.async_set_ble_device(service_info.device, service_info.advertisement)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            {"address": address},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    async def _async_update_options(
        _hass: HomeAssistant, updated_entry: ConfigEntry
    ) -> None:
        delay = updated_entry.options.get(
            CONF_DISCONNECT_DELAY, DEFAULT_DISCONNECT_DELAY
        )
        await updated_entry.runtime_data.coordinator.async_set_disconnect_delay(delay)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    await hass.config_entries.async_forward_entry_setups(entry, [Platform.LIGHT])
    entry.async_create_background_task(
        hass,
        coordinator.async_refresh(),
        "Surplife BLE initial refresh",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entity and close any BLE connection."""
    from homeassistant.const import Platform

    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, [Platform.LIGHT]
    ):
        await entry.runtime_data.coordinator.async_disconnect()
    return unload_ok

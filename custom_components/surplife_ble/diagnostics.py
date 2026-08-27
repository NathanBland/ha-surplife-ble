"""Diagnostics support for Surplife BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import SurplifeRuntimeData


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data: SurplifeRuntimeData = entry.runtime_data
    coordinator = runtime_data.coordinator
    state = coordinator.data

    return {
        "entry": {
            "title": entry.title,
            "address": coordinator.address,
            "version": entry.version,
        },
        "connected": coordinator.client is not None and coordinator.client.connected,
        "state": {
            "power": state.power if state else None,
            "brightness": state.brightness if state else None,
            "rgb": list(state.rgb) if state and state.rgb else None,
            "white": state.white if state else None,
            "assumed": state.assumed if state else None,
            "raw_hex": state.raw_hex if state else None,
            "led_count": state.led_count if state else None,
            "model_num": state.model_num if state else None,
            "firmware_version": state.firmware_version if state else None,
        },
        "resolution": {
            "codec": coordinator.client.resolution.codec
            if coordinator.client and coordinator.client.resolution
            else None,
            "transport": coordinator.client.resolution.transport
            if coordinator.client and coordinator.client.resolution
            else None,
            "repeatable": coordinator.client.resolution.repeatable
            if coordinator.client and coordinator.client.resolution
            else None,
        }
        if coordinator.client and coordinator.client.resolution
        else None,
    }

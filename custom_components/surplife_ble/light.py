"""Light entity for a verified Surplife BLE device."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_WHITE,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SurplifeCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one whole-lamp entity."""
    async_add_entities([SurplifeLight(entry.runtime_data.coordinator, entry)])


class SurplifeLight(CoordinatorEntity[SurplifeCoordinator], LightEntity):
    """Whole-lamp RGB/single-white controls only."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.WHITE}

    def __init__(self, coordinator: SurplifeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.address
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(dr.CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer="MIHEAL / Surplife",
            name=entry.title,
        )
        self._async_update_attrs()

    @callback
    def _async_update_attrs(self) -> None:
        state = self.coordinator.data
        if state is None:
            return
        self._attr_is_on = state.power
        self._attr_brightness = state.brightness
        self._attr_rgb_color = state.rgb
        # White mode is indicated by white > 0 (the IOTBT codec sets white=0
        # in color mode). RGB may be non-zero in white mode since HSV is still
        # computed, so we rely on the white field as the authoritative signal.
        is_white = bool(state.white and state.white > 0)
        self._attr_color_mode = ColorMode.WHITE if is_white else ColorMode.RGB
        self._attr_assumed_state = state.assumed
        model = (
            f"DX23012 ({state.model_num:#04x})"
            if state.model_num is not None
            else "DX23012"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            connections={(dr.CONNECTION_BLUETOOTH, self.coordinator.address)},
            manufacturer="MIHEAL / Surplife",
            model=model,
            name=self.coordinator.config_entry.title,
            sw_version=str(state.firmware_version) if state.firmware_version is not None else None,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = cast(int, kwargs.get(ATTR_BRIGHTNESS) or self.brightness or 255)
        if ATTR_RGB_COLOR in kwargs:
            await self.coordinator.async_set_rgb(kwargs[ATTR_RGB_COLOR], brightness)
        elif ATTR_WHITE in kwargs:
            await self.coordinator.async_set_white(cast(int, kwargs[ATTR_WHITE] or 255))
        elif ATTR_BRIGHTNESS in kwargs:
            await self.coordinator.async_set_brightness(brightness)
        else:
            await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()

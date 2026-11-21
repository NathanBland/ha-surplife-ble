"""Light entity for a verified Surplife BLE device."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_WHITE,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SurplifeCoordinator
from .core.products import product_for

SERVICE_SET_SEGMENTS = "set_segments"
ATTR_COLORS = "colors"

_RGB = vol.All(
    vol.ExactSequence((cv.byte, cv.byte, cv.byte)),
    lambda value: tuple(int(part) for part in value),
)
SET_SEGMENTS_SCHEMA = {
    vol.Required(ATTR_COLORS): vol.All(cv.ensure_list, vol.Length(min=1, max=255), [_RGB]),
    vol.Optional(ATTR_BRIGHTNESS): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one whole-lamp entity."""
    async_add_entities([SurplifeLight(entry.runtime_data.coordinator, entry)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_SEGMENTS, SET_SEGMENTS_SCHEMA, "async_set_segments"
    )


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
        client = self.coordinator.client
        product = product_for(state.product_id) if state.product_id is not None else None
        attributes: dict[str, Any] = {}
        if product is not None:
            attributes["product_id"] = product.product_id
            attributes["support_tier"] = product.tier
        if client is not None and client.strip_profile is not None:
            profile = client.strip_profile
            attributes["segment_count"] = profile.segment_count
            attributes["led_count"] = profile.point_count
            attributes["ic_type"] = profile.ic_type
            attributes["color_order"] = profile.color_order
        segments = client.segments if client is not None else None
        if segments is not None and not is_white:
            attributes["segment_colors"] = [list(rgb) for rgb in segments]
        self._attr_extra_state_attributes = attributes
        if product is not None:
            model = product.label
        elif state.model_num is not None:
            model = f"DX23012 ({state.model_num:#04x})"
        else:
            model = "DX23012"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            connections={(dr.CONNECTION_BLUETOOTH, self.coordinator.address)},
            manufacturer="MIHEAL / Surplife",
            model=model,
            name=self.coordinator.config_entry.title,
            sw_version=str(state.firmware_version) if state.firmware_version is not None else None,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        requested_brightness = cast(int | None, kwargs.get(ATTR_BRIGHTNESS))
        brightness = (
            requested_brightness
            if requested_brightness is not None
            else self.brightness if self.brightness is not None else 255
        )
        if ATTR_RGB_COLOR in kwargs:
            await self.coordinator.async_set_rgb(kwargs[ATTR_RGB_COLOR], brightness)
        elif ATTR_WHITE in kwargs:
            requested_white = cast(int | None, kwargs[ATTR_WHITE])
            await self.coordinator.async_set_white(
                requested_white if requested_white is not None else 255
            )
        elif ATTR_BRIGHTNESS in kwargs:
            await self.coordinator.async_set_brightness(brightness)
        else:
            await self.coordinator.async_set_power(True)

    async def async_set_segments(
        self, colors: list[tuple[int, int, int]], brightness: int | None = None
    ) -> None:
        """Give each lamp segment its own colour (entity service)."""
        client = self.coordinator.client
        resolution = client.resolution if client is not None else None
        if resolution is not None and "segments" not in resolution.capabilities:
            raise HomeAssistantError("this light does not support per-segment colour")
        try:
            await self.coordinator.async_set_segments(list(colors), brightness)
        except (RuntimeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()

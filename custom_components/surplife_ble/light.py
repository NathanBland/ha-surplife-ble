"""Light entity for a verified Surplife BLE device."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_WHITE,
    EFFECT_OFF,
    ColorMode,
    LightEntity,
    LightEntityFeature,
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
from .core.animations import ANIMATION_NAMES, DIRECTIONS
from .core.presets import PRESET_NAMES, matching_preset
from .core.products import product_for

SERVICE_SET_SEGMENTS = "set_segments"
SERVICE_SET_ANIMATION = "set_animation"
ATTR_COLORS = "colors"
ATTR_BACKGROUND_COLOR = "background_color"
ATTR_SPEED = "speed"
ATTR_DENSITY = "density"
ATTR_DIRECTION = "direction"

_RGB = vol.All(
    vol.ExactSequence((cv.byte, cv.byte, cv.byte)),
    lambda value: tuple(int(part) for part in value),
)
SET_SEGMENTS_SCHEMA = {
    vol.Required(ATTR_COLORS): vol.All(cv.ensure_list, vol.Length(min=1, max=255), [_RGB]),
    vol.Optional(ATTR_BRIGHTNESS): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
}

_PERCENT = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
SET_ANIMATION_SCHEMA = {
    vol.Required(ATTR_EFFECT): vol.In(ANIMATION_NAMES),
    vol.Optional(ATTR_SPEED): _PERCENT,
    vol.Optional(ATTR_DENSITY): _PERCENT,
    vol.Optional(ATTR_DIRECTION): vol.In(list(DIRECTIONS)),
    vol.Optional(ATTR_COLORS): vol.All(cv.ensure_list, vol.Length(min=1, max=16), [_RGB]),
    vol.Optional(ATTR_BACKGROUND_COLOR): _RGB,
    vol.Optional(ATTR_BRIGHTNESS): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the whole-lamp entity, plus segment entities once supported."""
    coordinator: SurplifeCoordinator = entry.runtime_data.coordinator
    async_add_entities([SurplifeLight(coordinator, entry)])
    added_segments = False

    @callback
    def _async_add_segments() -> None:
        nonlocal added_segments
        if added_segments or not _supports_segments(coordinator):
            return
        added_segments = True
        async_add_entities(
            SurplifeSegmentLight(coordinator, index)
            for index in range(coordinator.segment_count)
        )

    # Segment support is only known after the first poll identifies the lamp.
    entry.async_on_unload(coordinator.async_add_listener(_async_add_segments))
    _async_add_segments()
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_SEGMENTS, SET_SEGMENTS_SCHEMA, "async_set_segments"
    )
    platform.async_register_entity_service(
        SERVICE_SET_ANIMATION, SET_ANIMATION_SCHEMA, "async_set_animation"
    )


def _supports_segments(coordinator: SurplifeCoordinator) -> bool:
    client = coordinator.client
    resolution = client.resolution if client is not None else None
    return resolution is not None and "segments" in resolution.capabilities


def _device_info(coordinator: SurplifeCoordinator) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, coordinator.address)})


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
        if _supports_segments(self.coordinator):
            self._attr_supported_features = LightEntityFeature.EFFECT
            self._attr_effect_list = [EFFECT_OFF, *PRESET_NAMES, *ANIMATION_NAMES]
            animation = self.coordinator.animation
            if is_white:
                effect = None
            elif animation is not None:
                effect = animation.name
            else:
                effect = matching_preset(client.segments if client else None)
            self._attr_effect = effect or EFFECT_OFF
        else:
            self._attr_supported_features = LightEntityFeature(0)
            self._attr_effect_list = None
            self._attr_effect = None
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
        effect = cast(str | None, kwargs.get(ATTR_EFFECT))
        if effect is not None and effect != EFFECT_OFF:
            if effect in PRESET_NAMES:
                await self.coordinator.async_set_preset(effect, requested_brightness)
            elif effect in ANIMATION_NAMES:
                await self._async_animate(effect, brightness=requested_brightness)
            else:
                raise HomeAssistantError(f"unknown effect {effect}")
        elif effect == EFFECT_OFF and ATTR_RGB_COLOR not in kwargs:
            rgb = self.rgb_color or (255, 255, 255)
            await self.coordinator.async_set_rgb(rgb, brightness)
        elif ATTR_RGB_COLOR in kwargs:
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

    async def async_set_animation(
        self,
        effect: str,
        speed: int | None = None,
        density: int | None = None,
        direction: str | None = None,
        colors: list[tuple[int, int, int]] | None = None,
        background_color: tuple[int, int, int] | None = None,
        brightness: int | None = None,
    ) -> None:
        """Start a built-in animation with custom options (entity service)."""
        if not _supports_segments(self.coordinator):
            raise HomeAssistantError("this light does not support animations")
        await self._async_animate(
            effect,
            brightness=brightness,
            speed=speed,
            density=density,
            direction=direction,
            colors=list(colors) if colors else None,
            background=background_color,
        )

    async def _async_animate(self, effect: str, **options: Any) -> None:
        try:
            await self.coordinator.async_set_animation(effect, **options)
        except (RuntimeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()


class SurplifeSegmentLight(CoordinatorEntity[SurplifeCoordinator], LightEntity):
    """One addressable segment of a symphony lamp (segment 1 is the top).

    The lamp has a single brightness, so a segment's brightness is applied by
    scaling its colour.
    """

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_translation_key = "segment"

    def __init__(self, coordinator: SurplifeCoordinator, index: int) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"{coordinator.address}-segment-{index + 1}"
        self._attr_translation_placeholders = {"number": str(index + 1)}
        self._attr_device_info = _device_info(coordinator)
        self._async_update_attrs()

    @callback
    def _async_update_attrs(self) -> None:
        colors = self.coordinator.segment_colors()
        rgb = colors[self._index] if colors is not None and self._index < len(colors) else None
        state = self.coordinator.data
        self._attr_assumed_state = bool(state and state.assumed)
        if rgb is None or max(rgb) == 0:
            self._attr_is_on = False if state is not None else None
            return
        peak = max(rgb)
        self._attr_is_on = True
        self._attr_rgb_color = tuple(round(part * 255 / peak) for part in rgb)  # type: ignore[assignment]
        self._attr_brightness = peak

    async def async_turn_on(self, **kwargs: Any) -> None:
        rgb = cast(
            tuple[int, int, int],
            kwargs.get(ATTR_RGB_COLOR) or self.rgb_color or (255, 255, 255),
        )
        brightness = cast(int | None, kwargs.get(ATTR_BRIGHTNESS))
        if brightness is None:
            brightness = self.brightness if self.is_on and self.brightness else 255
        scaled = tuple(round(part * brightness / 255) for part in rgb)
        if max(scaled) == 0:
            scaled = (1, 1, 1)
        await self._async_set(scaled)  # type: ignore[arg-type]

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set((0, 0, 0))

    async def _async_set(self, rgb: tuple[int, int, int]) -> None:
        try:
            await self.coordinator.async_set_segment_color(self._index, rgb)
        except (RuntimeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()

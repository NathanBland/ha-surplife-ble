"""Bluetooth connection and state coordination for Surplife BLE."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import UPDATE_INTERVAL
from .core.client import LightClient
from .core.models import LightState, apply_assumed_rgb, rgb_state_matches

_LOGGER = logging.getLogger(__name__)
_ASSUMED_RGB_SECONDS = 5.0
_CONFIRMATION_DELAY_SECONDS = 0.3


@dataclass(slots=True)
class SurplifeRuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: SurplifeCoordinator


class SurplifeCoordinator(DataUpdateCoordinator[LightState]):
    """Manage the resolved client and expose normalized state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=UPDATE_INTERVAL,
            # BLE devices can't handle concurrent commands — serialize per device
            always_update=False,
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.client: LightClient | None = None
        self._ble_device: BLEDevice | None = None
        self._advertisement: AdvertisementData | None = None
        self._was_available: bool = False
        self._operation_lock = asyncio.Lock()
        self._assumed_rgb: tuple[int, int, int] | None = None
        self._assumed_rgb_brightness: int | None = None
        self._assumed_rgb_deadline: float | None = None
        self._confirmation_generation = 0

    def async_set_ble_device(
        self, device: BLEDevice, advertisement: AdvertisementData | None
    ) -> None:
        """Refresh the nearest-adapter BLEDevice from Home Assistant's cache."""
        self._ble_device = device
        self._advertisement = advertisement
        if self.client:
            self.client.set_ble_device(device, advertisement)

    async def _async_client_factory(self, device: BLEDevice) -> BleakClientWithServiceCache:
        """Use Home Assistant's retrying connection helper and service cache."""
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.config_entry.title,
            lambda disconnected: (
                self.client.mark_disconnected(disconnected) if self.client else None
            ),
            use_services_cache=True,
            ble_device_callback=lambda: self._ble_device or device,
        )

    async def _async_get_client(self) -> LightClient:
        if self._ble_device is None:
            self._ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address.upper(), True
            )
        if self._ble_device is None:
            raise UpdateFailed(f"Bluetooth device {self.address} is not currently reachable")
        if self.client is None:
            self.client = LightClient(
                self._ble_device,
                self._advertisement,
                client_factory=self._async_client_factory,
            )
        else:
            self.client.set_ble_device(self._ble_device, self._advertisement)
        await self.client.connect()
        return self.client

    def _clear_assumed_rgb(self) -> None:
        self._assumed_rgb = None
        self._assumed_rgb_brightness = None
        self._assumed_rgb_deadline = None

    def _reconcile_assumed_rgb(self, state: LightState) -> LightState:
        if self._assumed_rgb is None or self._assumed_rgb_brightness is None:
            state.assumed = False
            return state
        if rgb_state_matches(state, self._assumed_rgb, self._assumed_rgb_brightness):
            self._clear_assumed_rgb()
            state.assumed = False
            return state
        deadline = self._assumed_rgb_deadline
        if deadline is None or asyncio.get_running_loop().time() >= deadline:
            self._clear_assumed_rgb()
            state.assumed = False
            return state
        return apply_assumed_rgb(
            state,
            self._assumed_rgb,
            self._assumed_rgb_brightness,
        )

    async def _refresh_after_assumed_deadline(self, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        if self._assumed_rgb_deadline == deadline:
            await self.async_request_refresh()

    async def _refresh_after_command(self, generation: int) -> None:
        await asyncio.sleep(_CONFIRMATION_DELAY_SECONDS)
        if self._confirmation_generation == generation:
            await self.async_request_refresh()

    def _request_confirmation(self) -> None:
        self._confirmation_generation += 1
        self.hass.async_create_task(
            self._refresh_after_command(self._confirmation_generation)
        )
        if self._assumed_rgb_deadline is not None:
            self.hass.async_create_task(
                self._refresh_after_assumed_deadline(self._assumed_rgb_deadline)
            )

    async def _async_update_data(self) -> LightState:
        try:
            async with self._operation_lock:
                client = await self._async_get_client()
                state = await client.query_state()
                if state is None:
                    raise UpdateFailed("device did not return a supported state frame")
                if not client.resolution or not client.resolution.repeatable:
                    raise UpdateFailed("device state response could not be repeated")
                state = self._reconcile_assumed_rgb(state)
                if not self._was_available:
                    _LOGGER.info("Surplife BLE device %s is now available", self.address)
                    self._was_available = True
                return state
        except UpdateFailed:
            if self._was_available:
                _LOGGER.warning("Surplife BLE device %s is unavailable", self.address)
                self._was_available = False
            # Do not disconnect on transient errors. The bleak_retry_connector
            # will reconnect on the next attempt. Disconnecting here forces a
            # full reconnect (discover, subscribe, probe) on every poll failure,
            # which makes commands slow.
            raise
        except Exception as err:
            if self._was_available:
                _LOGGER.warning("Surplife BLE device %s is unavailable: %s", self.address, err)
                self._was_available = False
            # Do not disconnect on transient errors. Let the next poll or
            # command retry with the existing connection state.
            raise UpdateFailed(str(err)) from err

    async def _async_command(self, method: str, *args: Any, **kwargs: Any) -> LightState:
        async with self._operation_lock:
            try:
                client = await self._async_get_client()
                if not client.resolution or not client.resolution.repeatable:
                    await client.query_state()
                command = getattr(client, method)
                await command(*args, **kwargs)
            except (BleakError, TimeoutError, OSError) as err:
                _LOGGER.debug("Command %s failed, retrying: %s", method, err)
                if self.client:
                    with suppress(Exception):
                        await self.client.disconnect()
                client = await self._async_get_client()
                if not client.resolution or not client.resolution.repeatable:
                    await client.query_state()
                command = getattr(client, method)
                await command(*args, **kwargs)
            assert client.state is not None
            self.async_set_updated_data(client.state)
            return client.state

    async def async_set_power(self, turn_on: bool) -> None:
        await self._async_command("set_power", turn_on)

    async def async_set_rgb(
        self, rgb: tuple[int, int, int], brightness: int | None
    ) -> None:
        if brightness is None:
            brightness = 255
        state = await self._async_command(
            "set_rgb", *rgb, brightness=max(1, round(brightness / 255 * 100))
        )
        if state.assumed and state.rgb is not None and state.brightness is not None:
            self._assumed_rgb = state.rgb
            self._assumed_rgb_brightness = state.brightness
            self._assumed_rgb_deadline = (
                asyncio.get_running_loop().time() + _ASSUMED_RGB_SECONDS
            )
            self._request_confirmation()
        else:
            self._clear_assumed_rgb()

    async def async_set_white(self, brightness: int | None) -> None:
        if brightness is None:
            brightness = 255
        await self._async_command("set_white", max(1, round(brightness / 255 * 100)))
        self._clear_assumed_rgb()
        self._request_confirmation()

    async def async_set_brightness(self, brightness: int | None) -> None:
        if brightness is None:
            brightness = 255
        state = await self._async_command(
            "set_brightness", max(1, round(brightness / 255 * 100))
        )
        if state.assumed:
            if not state.white and state.rgb is not None and state.brightness is not None:
                self._assumed_rgb = state.rgb
                self._assumed_rgb_brightness = state.brightness
                self._assumed_rgb_deadline = (
                    asyncio.get_running_loop().time() + _ASSUMED_RGB_SECONDS
                )
            else:
                self._clear_assumed_rgb()
            self._request_confirmation()
        else:
            self._clear_assumed_rgb()

    async def async_disconnect(self) -> None:
        if self.client:
            await self.client.disconnect()

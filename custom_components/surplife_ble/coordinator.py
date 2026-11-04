"""Bluetooth connection and state coordination for Surplife BLE."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
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
# Upper bound for the GATT work of one poll or command once connected. The
# connection itself is bounded by bleak_retry_connector, which needs longer on
# Bluetooth proxies, so it is not included.
_OPERATION_TIMEOUT_SECONDS = 15.0
_DISCONNECT_TIMEOUT_SECONDS = 10.0
# A link can stay "connected" while the lamp stops answering. Drop it after this
# many failed polls in a row, so the next poll makes a fresh connection.
_RECONNECT_AFTER_FAILURES = 2
# Keep showing the last known state (marked assumed) for this many failed polls
# in a row before the entity becomes unavailable.
_TOLERATED_FAILURES = 3
# Wait between attempts to identify an unresolved device, doubling up to the cap.
_PROBE_BACKOFF_BASE_SECONDS = 15.0
_PROBE_BACKOFF_MAX_SECONDS = 300.0


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
        self._consecutive_failures = 0
        self._probe_failures = 0
        self._next_probe_at: float | None = None

    def async_set_ble_device(
        self, device: BLEDevice, advertisement: AdvertisementData | None
    ) -> None:
        """Refresh the nearest-adapter BLEDevice from Home Assistant's cache."""
        self._ble_device = device
        self._advertisement = advertisement
        if self.client:
            self.client.set_ble_device(device, advertisement)

    def _current_ble_device(self, fallback: BLEDevice | None = None) -> BLEDevice | None:
        return (
            bluetooth.async_ble_device_from_address(
                self.hass,
                self.address.upper(),
                True,
            )
            or self._ble_device
            or fallback
        )

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
            ble_device_callback=lambda: self._current_ble_device(device) or device,
        )

    async def _async_get_client(self) -> LightClient:
        if current_device := self._current_ble_device():
            self._ble_device = current_device
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

    async def _async_reset_connection(self, *, clear_cache: bool = False) -> None:
        """Drop the BLE connection so the next operation reconnects from scratch."""
        if self.client is None:
            return
        if clear_cache:
            await self.client.clear_cache()
        with suppress(Exception):
            async with asyncio.timeout(_DISCONNECT_TIMEOUT_SECONDS):
                await self.client.disconnect()

    def _probe_backoff_active(self) -> bool:
        return (
            self._next_probe_at is not None
            and asyncio.get_running_loop().time() < self._next_probe_at
        )

    def _record_probe_failure(self) -> None:
        self._probe_failures += 1
        delay = min(
            _PROBE_BACKOFF_BASE_SECONDS * 2 ** (self._probe_failures - 1),
            _PROBE_BACKOFF_MAX_SECONDS,
        )
        self._next_probe_at = asyncio.get_running_loop().time() + delay

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._probe_failures = 0
        self._next_probe_at = None
        if not self._was_available:
            _LOGGER.info("Surplife BLE device %s is now available", self.address)
            self._was_available = True

    async def _async_poll(self) -> LightState:
        client = await self._async_get_client()
        was_resolved = client.resolved
        async with asyncio.timeout(_OPERATION_TIMEOUT_SECONDS):
            # Once resolved, the lamp answers on FF02 notifications; polling every
            # readable characteristic after a slow reply only adds GATT traffic.
            state = await client.query_state(allow_reads=not was_resolved)
        if state is None:
            raise UpdateFailed("device did not return a supported state frame")
        if not client.resolution or not client.resolution.repeatable:
            raise UpdateFailed("device state response could not be repeated")
        return state

    async def _async_update_data(self) -> LightState:
        async with self._operation_lock:
            client = self.client
            if client is not None and not client.resolved and self._probe_backoff_active():
                # Probing writes a burst of guessed frames. Repeating it on every
                # poll keeps the lamp busy and can make it disconnect.
                raise UpdateFailed("waiting before identifying the device again")
            try:
                state = await self._async_poll()
            except Exception as err:
                return await self._async_handle_poll_failure(err)
            self._record_success()
            return self._reconcile_assumed_rgb(state)

    async def _async_handle_poll_failure(self, err: Exception) -> LightState:
        self._consecutive_failures += 1
        if self.client is not None and not self.client.resolved:
            self._record_probe_failure()
        if isinstance(err, BleakError):
            # Stale cached services make every write fail the same way, so drop
            # them along with the link.
            await self._async_reset_connection(clear_cache=True)
        elif self._consecutive_failures >= _RECONNECT_AFTER_FAILURES:
            _LOGGER.debug(
                "Surplife BLE device %s failed %s polls in a row; reconnecting",
                self.address,
                self._consecutive_failures,
            )
            await self._async_reset_connection()

        if self.data is not None and self._consecutive_failures < _TOLERATED_FAILURES:
            _LOGGER.debug(
                "Surplife BLE poll of %s failed (%s/%s), keeping last state: %s",
                self.address,
                self._consecutive_failures,
                _TOLERATED_FAILURES,
                err,
            )
            return replace(self.data, assumed=True)

        if self._was_available:
            _LOGGER.warning("Surplife BLE device %s is unavailable: %s", self.address, err)
            self._was_available = False
        if isinstance(err, UpdateFailed):
            raise err
        raise UpdateFailed(str(err) or type(err).__name__) from err

    async def _async_run_command(self, method: str, *args: Any, **kwargs: Any) -> None:
        client = await self._async_get_client()
        async with asyncio.timeout(_OPERATION_TIMEOUT_SECONDS):
            if not client.resolution or not client.resolution.repeatable:
                await client.query_state()
            await getattr(client, method)(*args, **kwargs)

    async def _async_command(self, method: str, *args: Any, **kwargs: Any) -> LightState:
        async with self._operation_lock:
            try:
                await self._async_run_command(method, *args, **kwargs)
            except (BleakError, TimeoutError, OSError) as err:
                _LOGGER.debug("Command %s failed, reconnecting and retrying: %s", method, err)
                await self._async_reset_connection(clear_cache=isinstance(err, BleakError))
                await self._async_run_command(method, *args, **kwargs)
            assert self.client is not None and self.client.state is not None
            self._record_success()
            self.async_set_updated_data(self.client.state)
            return self.client.state

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
        await self._async_reset_connection()

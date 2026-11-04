from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.surplife_ble.core.models import LightState


@pytest.fixture(autouse=True)
def _clear_coordinator_module() -> Any:
    yield
    sys.modules.pop("custom_components.surplife_ble.coordinator", None)


def _load_coordinator(monkeypatch: pytest.MonkeyPatch) -> Any:
    homeassistant = ModuleType("homeassistant")
    components = ModuleType("homeassistant.components")
    bluetooth = ModuleType("homeassistant.components.bluetooth")
    config_entries = ModuleType("homeassistant.config_entries")
    const = ModuleType("homeassistant.const")
    core = ModuleType("homeassistant.core")
    helpers = ModuleType("homeassistant.helpers")
    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item: object) -> type[DataUpdateCoordinator]:
            return cls

        def __init__(
            self,
            hass: object,
            _logger: logging.Logger,
            *,
            config_entry: object,
            **_kwargs: object,
        ) -> None:
            self.hass = hass
            self.config_entry = config_entry

        async def async_request_refresh(self) -> None:
            return None

        def async_set_updated_data(self, data: LightState) -> None:
            self.data = data

    class UpdateFailed(Exception):
        pass

    bluetooth.async_ble_device_from_address = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    config_entries.ConfigEntry = object  # type: ignore[attr-defined]
    const.CONF_ADDRESS = "address"  # type: ignore[attr-defined]
    core.HomeAssistant = object  # type: ignore[attr-defined]
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator  # type: ignore[attr-defined]
    update_coordinator.UpdateFailed = UpdateFailed  # type: ignore[attr-defined]
    components.bluetooth = bluetooth  # type: ignore[attr-defined]
    homeassistant.components = components  # type: ignore[attr-defined]
    homeassistant.config_entries = config_entries  # type: ignore[attr-defined]
    homeassistant.const = const  # type: ignore[attr-defined]
    homeassistant.core = core  # type: ignore[attr-defined]
    homeassistant.helpers = helpers  # type: ignore[attr-defined]
    helpers.update_coordinator = update_coordinator  # type: ignore[attr-defined]

    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.bluetooth": bluetooth,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("custom_components.surplife_ble.coordinator", None)
    return importlib.import_module("custom_components.surplife_ble.coordinator")


class FakeHass:
    def async_create_task(self, coroutine: object) -> None:
        coroutine.close()  # type: ignore[attr-defined]


class FakeEntry:
    title = "Test light"
    data = {"address": "DEVICE"}


def test_current_ble_device_uses_home_assistant_best_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_coordinator(monkeypatch)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    cached = object()
    current = object()
    coordinator._ble_device = cached
    monkeypatch.setattr(
        module.bluetooth,
        "async_ble_device_from_address",
        lambda *_args, **_kwargs: current,
    )

    assert coordinator._current_ble_device() is current


@pytest.mark.asyncio
async def test_assumed_rgb_is_cleared_when_poll_confirms_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_coordinator(monkeypatch)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    coordinator._assumed_rgb = (0, 0, 255)
    coordinator._assumed_rgb_brightness = 102
    coordinator._assumed_rgb_deadline = asyncio.get_running_loop().time() + 5

    state = coordinator._reconcile_assumed_rgb(
        LightState(power=True, rgb=(0, 0, 102), brightness=102, white=0)
    )

    assert not state.assumed
    assert coordinator._assumed_rgb is None


@pytest.mark.asyncio
async def test_assumed_rgb_expires_and_returns_device_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_coordinator(monkeypatch)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    coordinator._assumed_rgb = (0, 0, 255)
    coordinator._assumed_rgb_brightness = 102
    coordinator._assumed_rgb_deadline = asyncio.get_running_loop().time() - 1

    state = coordinator._reconcile_assumed_rgb(
        LightState(power=True, rgb=(102, 0, 0), brightness=102, white=0)
    )

    assert state.rgb == (102, 0, 0)
    assert not state.assumed
    assert coordinator._assumed_rgb is None


@pytest.mark.asyncio
async def test_background_confirmation_skips_superseded_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_coordinator(monkeypatch)
    monkeypatch.setattr(module, "_CONFIRMATION_DELAY_SECONDS", 0)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1

    coordinator.async_request_refresh = refresh
    coordinator._confirmation_generation = 2
    await coordinator._refresh_after_command(1)
    await coordinator._refresh_after_command(2)
    assert refreshes == 1


@pytest.mark.asyncio
async def test_command_does_not_retry_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_coordinator(monkeypatch)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    calls = 0

    class FakeClient:
        resolved = True
        resolution = SimpleNamespace(repeatable=True)
        state = LightState()

        async def set_rgb(self) -> None:
            nonlocal calls
            calls += 1
            raise ValueError("invalid command")

    client = FakeClient()

    async def get_client() -> FakeClient:
        return client

    coordinator._async_get_client = get_client
    with pytest.raises(ValueError, match="invalid command"):
        await coordinator._async_command("set_rgb")
    assert calls == 1


class PollingFakeClient:
    """A resolved client whose state queries can be made to fail."""

    def __init__(self) -> None:
        self.resolved = True
        self.resolution = SimpleNamespace(repeatable=True)
        self.state = LightState(power=True, brightness=100)
        self.error: Exception | None = None
        self.queries = 0
        self.disconnects = 0
        self.cache_clears = 0

    async def query_state(self, **_kwargs: object) -> LightState | None:
        self.queries += 1
        if self.error is not None:
            raise self.error
        return self.state

    async def disconnect(self) -> None:
        self.disconnects += 1

    async def clear_cache(self) -> None:
        self.cache_clears += 1


def _polling_coordinator(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, PollingFakeClient]:
    module = _load_coordinator(monkeypatch)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    client = PollingFakeClient()
    coordinator.client = client
    coordinator.data = None

    async def get_client() -> PollingFakeClient:
        return client

    coordinator._async_get_client = get_client
    return module, coordinator, client


@pytest.mark.asyncio
async def test_silent_link_is_reconnected_and_last_state_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, coordinator, client = _polling_coordinator(monkeypatch)
    coordinator.data = await coordinator._async_update_data()

    client.error = module.UpdateFailed("device did not return a supported state frame")
    first = await coordinator._async_update_data()
    assert first.assumed and first.power
    assert client.disconnects == 0

    second = await coordinator._async_update_data()
    assert second.assumed
    assert client.disconnects == 1

    with pytest.raises(module.UpdateFailed):
        await coordinator._async_update_data()

    client.error = None
    recovered = await coordinator._async_update_data()
    assert not recovered.assumed
    assert coordinator._consecutive_failures == 0


@pytest.mark.asyncio
async def test_bleak_error_clears_cache_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, coordinator, client = _polling_coordinator(monkeypatch)
    client.error = module.BleakError("write failed")
    with pytest.raises(module.UpdateFailed):
        await coordinator._async_update_data()
    assert client.cache_clears == 1
    assert client.disconnects == 1


@pytest.mark.asyncio
async def test_unresolved_device_is_not_probed_on_every_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, coordinator, client = _polling_coordinator(monkeypatch)
    client.resolved = False
    client.error = module.UpdateFailed("no supported state frame")
    with pytest.raises(module.UpdateFailed):
        await coordinator._async_update_data()
    with pytest.raises(module.UpdateFailed, match="waiting"):
        await coordinator._async_update_data()
    assert client.queries == 1

    coordinator._next_probe_at = asyncio.get_running_loop().time() - 1
    with pytest.raises(module.UpdateFailed):
        await coordinator._async_update_data()
    assert client.queries == 2
    # The wait doubles after each failed attempt.
    assert coordinator._next_probe_at - asyncio.get_running_loop().time() > 20


@pytest.mark.asyncio
async def test_probe_gets_a_longer_budget_than_a_resolved_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, coordinator, client = _polling_coordinator(monkeypatch)
    monkeypatch.setattr(module, "_OPERATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(module, "_PROBE_TIMEOUT_SECONDS", 1.0)

    async def slow_query(**_kwargs: object) -> LightState:
        await asyncio.sleep(0.05)
        return client.state

    client.query_state = slow_query
    client.resolved = False
    assert (await coordinator._async_update_data()).power

    client.resolved = True
    with pytest.raises(module.UpdateFailed):
        await coordinator._async_update_data()

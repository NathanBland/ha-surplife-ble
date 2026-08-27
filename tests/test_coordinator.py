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

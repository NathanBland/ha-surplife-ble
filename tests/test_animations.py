"""E1 01 symphony animations: encoding, client state and coordinator defaults."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.surplife_ble.core.animations import (
    ANIMATION_NAMES,
    DEFAULT_COLORS,
    AnimationCommand,
    animation_for_model,
    animation_named,
)
from custom_components.surplife_ble.core.models import LightState
from custom_components.surplife_ble.core.presets import PRESET_NAMES
from custom_components.surplife_ble.core.protocol import iotbt_e1_animation, parse_state
from tests.test_coordinator import FakeEntry, FakeHass, _load_coordinator
from tests.test_segments import FakeLamp, _client


def test_animation_header_matches_app_layout() -> None:
    payload = iotbt_e1_animation(
        9,
        [(255, 0, 0), (0, 0, 255)],
        brightness=80,
        speed=50,
        density=25,
        direction=2,
        model_expand=1,
    )
    assert payload[:10] == bytes((0xE1, 0x01, 0x00, 80, 9, 1, 2, 25, 50, 0x00))
    # Black background record, then the count and one record per colour.
    assert payload[10:14] == bytes((0xA1, 0x00, 0x00, 0x00))
    assert payload[14] == 2
    # Red: hue 0, saturation 100, value 100.
    assert payload[15:19] == bytes((0xA1, 0x00, 100, 100))
    # Blue: hue 240 packed with saturation 100 as (240 << 7 | 100).
    packed = 240 << 7 | 100
    assert payload[19:23] == bytes((0xA1, packed >> 8, packed & 0xFF, 100))
    assert len(payload) == 23


def test_animation_record_value_follows_colour_peak() -> None:
    payload = iotbt_e1_animation(1, [(0, 128, 0)], background=(0, 0, 255))
    assert payload[13] == 100
    assert payload[18] == 50


def test_animation_clamps_and_validates() -> None:
    payload = iotbt_e1_animation(1, [(255, 0, 0)], brightness=300, speed=-5, density=150)
    assert payload[3] == 100 and payload[7] == 100 and payload[8] == 0
    with pytest.raises(ValueError):
        iotbt_e1_animation(0, [(255, 0, 0)])
    with pytest.raises(ValueError):
        iotbt_e1_animation(1, [])
    with pytest.raises(ValueError):
        iotbt_e1_animation(1, [(1, 1, 1)] * 17)


def test_animation_table_has_breathe_and_unique_names() -> None:
    assert animation_named("Breathe").model == 9
    assert animation_named("Sync gradient").model == 77
    assert len(set(ANIMATION_NAMES)) == len(ANIMATION_NAMES)
    assert not set(ANIMATION_NAMES) & set(PRESET_NAMES)
    with pytest.raises(ValueError):
        animation_named("Disco")


@pytest.mark.asyncio
async def test_client_sends_animation_and_brightness_resends_it() -> None:
    fake = FakeLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_segments([(255, 0, 0)], brightness=60)
        command = AnimationCommand(model=9, colors=((255, 0, 0), (0, 0, 255)), brightness=70)
        await client.set_animation(command)
        sent = next(d for _u, d, _r in fake.writes if d[8:10] == b"\xe1\x01")
        assert sent[7] == 0x0B
        assert sent[8:] == iotbt_e1_animation(9, [(255, 0, 0), (0, 0, 255)], brightness=70)
        assert client.animation == command
        assert client.segments is None
        assert client.state is not None and client.state.power is True
        # Brightness re-sends the animation rather than a solid colour.
        before = len(fake.writes)
        await client.set_brightness(30)
        resent = [d for _u, d, _r in fake.writes[before:] if d[8:10] == b"\xe1\x01"]
        assert resent and resent[0][11] == 30
        assert client.animation is not None and client.animation.brightness == 30
        # A solid colour stops it.
        await client.set_rgb(0, 255, 0)
        assert client.animation is None


def _animation_coordinator(monkeypatch: pytest.MonkeyPatch, state: LightState) -> Any:
    module = _load_coordinator(monkeypatch)
    coordinator = module.SurplifeCoordinator(FakeHass(), FakeEntry())
    coordinator.data = state
    calls: list[tuple[Any, ...]] = []

    class Client:
        animation: AnimationCommand | None = None
        segments = None
        segment_count = 4

    coordinator.client = Client()

    async def command(method: str, *args: Any, **_kwargs: Any) -> None:
        calls.append((method, *args))
        if method == "set_animation":
            coordinator.client.animation = args[0]

    async def set_power(turn_on: bool) -> None:
        calls.append(("power", turn_on))

    coordinator._async_command = command
    coordinator.async_set_power = set_power
    return coordinator, calls


@pytest.mark.asyncio
async def test_coordinator_fills_app_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, calls = _animation_coordinator(
        monkeypatch, LightState(power=True, rgb=(255, 0, 0), brightness=255, white=0)
    )
    await coordinator.async_set_animation("Sharp flash")
    command = calls[-1][1]
    assert command == AnimationCommand(
        model=11,
        colors=DEFAULT_COLORS,
        brightness=100,
        speed=50,
        density=25,
        direction=1,
        model_expand=1,
        name="Sharp flash",
    )
    # Segment lights show nothing meaningful while the lamp animates.
    assert coordinator.segment_colors() is None


@pytest.mark.asyncio
async def test_coordinator_keeps_running_options_when_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, calls = _animation_coordinator(
        monkeypatch, LightState(power=True, rgb=(255, 0, 0), brightness=128, white=0)
    )
    await coordinator.async_set_animation(
        "Wave", speed=80, direction="left", colors=[(255, 0, 0)], background=(0, 0, 40)
    )
    await coordinator.async_set_animation("Async breathe")
    command = calls[-1][1]
    assert command.model == 75
    assert command.speed == 80
    assert command.direction == 2
    assert command.colors == ((255, 0, 0),)
    assert command.background == (0, 0, 40)
    assert command.density == 40
    assert command.brightness == 50
    with pytest.raises(ValueError):
        await coordinator.async_set_animation("Wave", direction="sideways")


@pytest.mark.asyncio
async def test_animation_powers_on_an_off_lamp_first(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, calls = _animation_coordinator(
        monkeypatch, LightState(power=False, rgb=(255, 0, 0), brightness=255, white=0)
    )
    await coordinator.async_set_animation("Breathe")
    assert [call[0] for call in calls] == ["power", "set_animation"]



def test_service_effect_options_match_animation_table() -> None:
    from pathlib import Path

    text = (
        Path(__file__).parent.parent / "custom_components/surplife_ble/services.yaml"
    ).read_text()
    effect_block = text.split("set_animation:", 1)[1].split("    speed:", 1)[0]
    options = [
        line.strip()[2:].strip('"')
        for line in effect_block.splitlines()
        if line.strip().startswith("- ")
    ]
    assert options == ANIMATION_NAMES



WHITE_STATE = "ea810100aa0923256f32f0006464006405001dffffffff01000000"
ANIMATING_STATE = "ea810100aa0923250914f0aae464006405001dffffffff01000000"
COLOUR_STATE = "ea810100aa0923240150f0786464006405001dffffffff01000000"


def test_state_reports_running_animation_not_white() -> None:
    animating = parse_state(bytes.fromhex(ANIMATING_STATE))
    assert animating is not None
    assert animating.animation_model == 9
    assert animating.white == 0
    assert animating.rgb == (255, 0, 81)
    white = parse_state(bytes.fromhex(WHITE_STATE))
    assert white is not None and white.animation_model is None and white.white == 255
    colour = parse_state(bytes.fromhex(COLOUR_STATE))
    assert colour is not None and colour.animation_model is None and colour.white == 0
    assert animation_for_model(9) is not None and animation_for_model(111) is None


class AnimatingLamp(FakeLamp):
    """Lamp that reports E1 01 animations in its state like the real one."""

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        body = bytes(data)[8:]
        if body[:2] == b"\xe1\x01":
            state = bytearray(self.state)
            state[7], state[8], state[9] = 0x25, body[4], body[8]
            self.state = bytes(state)
        await super().write_gatt_char(uuid, data, response)


@pytest.mark.asyncio
async def test_poll_keeps_animation_and_its_brightness() -> None:
    fake = AnimatingLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_animation(AnimationCommand(model=9, colors=((255, 0, 0),), brightness=50))
        polled = await client.query_state(response_timeout=0.5)
        assert polled is not None
        assert polled.animation_model == 9
        assert polled.white == 0
        assert polled.brightness == 128
        assert client.animation is not None and client.animation.model == 9


@pytest.mark.asyncio
async def test_animation_is_forgotten_when_lamp_leaves_it() -> None:
    fake = AnimatingLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_animation(AnimationCommand(model=9, colors=((255, 0, 0),)))
        fake.state = bytes.fromhex(COLOUR_STATE)
        await client.query_state(response_timeout=0.5)
        assert client.animation is None


@pytest.mark.asyncio
async def test_effect_name_comes_from_lamp_state_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _calls = _animation_coordinator(
        monkeypatch, LightState(power=True, rgb=(255, 0, 0), brightness=255, white=0)
    )
    assert coordinator.animation_name() is None
    coordinator.data = LightState(power=True, rgb=(255, 0, 0), white=0, animation_model=77)
    assert coordinator.animation_name() == "Sync gradient"
    assert coordinator.segment_colors() is None

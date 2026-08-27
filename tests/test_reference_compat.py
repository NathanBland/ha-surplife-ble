"""Differential checks against the released reference libraries."""

from flux_led.protocol import ProtocolLEDENET9Byte
from led_ble.const import STATE_COMMAND

from custom_components.surplife_ble.core.protocol import (
    QUERY_CLASSIC,
    QUERY_LED_BLE,
    classic_power,
    classic_rgb,
)


def test_released_led_ble_probe_matches() -> None:
    assert bytes(STATE_COMMAND) == QUERY_LED_BLE


def test_released_flux_led_classic_commands_match() -> None:
    protocol = ProtocolLEDENET9Byte()
    assert bytes(protocol.construct_state_query()) == QUERY_CLASSIC
    assert bytes(protocol.construct_state_change(1)) == classic_power(True)
    expected_rgb = protocol.construct_levels_change(
        persist=1,
        red=255,
        green=0,
        blue=0,
        warm_white=0,
        cool_white=0,
        write_mode=0xF0,
    )[0]
    assert bytes(expected_rgb) == classic_rgb(255, 0, 0, nine_byte=True)

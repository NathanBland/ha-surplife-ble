"""MagicHome2 BLE product table from the Surplife 1.6.5 Android app.

Generated once from the app's ``ble_devices.json`` and reviewed by hand. The
APK data is not read at runtime. The product ID is byte 4 of an ``EA 81``
state frame.

Support tiers (see docs/protocol-apk-1.6.5.md section 8):

- ``symphony-e1``: same protocol set as the verified DX23012 floor lamp
  (product 170). Whole-lamp and per-segment control use the same commands.
- every other product is recognised by name only; control still depends on
  the state layout the protocol classifier verifies.
"""

from __future__ import annotations

from dataclasses import dataclass

TIER_SYMPHONY_E1 = "symphony-e1"
TIER_RECOGNISED = "recognised"

# Products whose EA 81 byte 4 the older flux_led reference already assigns to
# a different extended-custom codec. Their handling is left unchanged until
# hardware shows which interpretation is right.
_REFERENCE_EXTENDED_IDS = frozenset({0xB6})

# Retail names for products confirmed on hardware.
_PRODUCT_NAMES = {170: "MIHEAL DX23012 floor lamp"}


@dataclass(frozen=True, slots=True)
class SurplifeProduct:
    """One BLE product entry from the app's device table."""

    product_id: int
    category: str
    protocols: tuple[str, ...]
    state_protocols: tuple[str, ...]

    @property
    def supports_segments(self) -> bool:
        return "symphonyE1" in self.protocols and "wifibleSymphony" in self.state_protocols

    @property
    def tier(self) -> str:
        return TIER_SYMPHONY_E1 if is_symphony_e1_product(self.product_id) else TIER_RECOGNISED

    @property
    def label(self) -> str:
        name = _PRODUCT_NAMES.get(self.product_id)
        if name is not None:
            return f"{name} (product {self.product_id})"
        return f"MagicHome2 {self.category} product {self.product_id}"


PRODUCTS: dict[int, SurplifeProduct] = {
    6: SurplifeProduct(
        6,
        "dj",
        ("common", "rgbw_mini", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    7: SurplifeProduct(
        7,
        "dj",
        ("common", "rgbcw_mini", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    8: SurplifeProduct(
        8,
        "dj",
        ("common", "rgb_mini_mic", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    9: SurplifeProduct(9, "dj2", ("common", "cct_high_pressure"), ("wifibleLightStandardV1_1",)),
    14: SurplifeProduct(
        14,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    16: SurplifeProduct(16, "sdd", ("common", "christmas_wifi"), ("wifibleChristmasLight",)),
    22: SurplifeProduct(22, "swdj", ("common", "common1_0"), ("wifibleCCT",)),
    23: SurplifeProduct(
        23, "dsdj", ("common", "common1_0"), ("wifibleBrightness", "wifibleLightStandardV2")
    ),
    26: SurplifeProduct(26, "sdd", ("common", "christmas_wifi"), ("wifibleChristmasLight",)),
    28: SurplifeProduct(28, "dj2", ("common", "cct"), ("wifibleLightStandardV1_1",)),
    30: SurplifeProduct(
        30,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    33: SurplifeProduct(
        33,
        "dsdj",
        ("common", "common1_0", "common2_0"),
        ("wifibleBrightness", "wifibleLightStandardV2"),
    ),
    41: SurplifeProduct(
        41,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    51: SurplifeProduct(
        51,
        "dj",
        ("common", "rgb_mini", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    53: SurplifeProduct(
        53,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    54: SurplifeProduct(54, "swdj", ("common", "common1_0"), ("wifibleCCT",)),
    55: SurplifeProduct(55, "swdj", ("common", "common1_0"), ("wifibleCCT",)),
    57: SurplifeProduct(
        57, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    60: SurplifeProduct(
        60,
        "dj",
        ("common", "rgb_mini_mic", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    61: SurplifeProduct(
        61,
        "hc3",
        ("common", "common1_0", "common2_0", "symphonyE1"),
        ("wifibleSymphonyCeilingLamp",),
    ),
    62: SurplifeProduct(
        62,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    65: SurplifeProduct(
        65,
        "dsdj",
        ("common", "common1_0", "common2_0"),
        ("wifibleBrightness", "wifibleLightStandardV2"),
    ),
    68: SurplifeProduct(68, "dj", ("common", "common1_0"), ("wifibleLightStandardV1",)),
    69: SurplifeProduct(
        69, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    70: SurplifeProduct(70, "swdj", ("common", "common1_0"), ("wifibleCCT",)),
    71: SurplifeProduct(71, "hc3", ("common", "common1_0", "common2_0"), ("wifibleChristmasCC",)),
    72: SurplifeProduct(
        72,
        "dj",
        ("common", "rgbw_mini", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    73: SurplifeProduct(73, "hc3", ("common", "common1_0", "common2_0"), ("wifibleChristmasCC",)),
    76: SurplifeProduct(
        76, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    77: SurplifeProduct(
        77,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    79: SurplifeProduct(79, "dj", ("common", "common1_0", "common2_0"), ("wifibleMusicBox",)),
    81: SurplifeProduct(81, "pmzk", ("common2_0",), ("wifibleLightStandardV2",)),
    82: SurplifeProduct(82, "swdj", ("common", "common1_0"), ("wifibleCCT",)),
    83: SurplifeProduct(
        83, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    84: SurplifeProduct(84, "dj", ("common", "rgbw"), ("wifibleLightStandardV1",)),
    85: SurplifeProduct(
        85,
        "dj",
        ("common", "common1_0", "common2_0"),
        ("wifibleLightStandardV1", "wifibleLightStandardV2"),
    ),
    86: SurplifeProduct(
        86, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    98: SurplifeProduct(
        98, "swdj", ("common", "common1_0", "common2_0"), ("wifibleCCT", "wifibleLightStandardV2")
    ),
    151: SurplifeProduct(151, "dk", ("common", "common2_0"), ("wifibleSwitch", "wifibleSwitchV2")),
    161: SurplifeProduct(161, "hc1", ("common", "symphony_wifi"), ("wifibleRGBSymphony",)),
    162: SurplifeProduct(162, "hc2", ("common", "symphony_wifi_new"), ("wifibleNewRGBSymphony",)),
    163: SurplifeProduct(163, "hc2", ("common", "symphony_wifi_new"), ("wifibleNewRGBSymphony",)),
    164: SurplifeProduct(164, "hc2", ("common", "symphony_wifi"), ("wifibleNewRGBSymphony",)),
    166: SurplifeProduct(166, "hc2", ("common", "symphony_wifi"), ("wifibleNewRGBSymphony",)),
    170: SurplifeProduct(
        170, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    171: SurplifeProduct(
        171, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    172: SurplifeProduct(
        172,
        "hc3",
        ("common", "common1_0", "common2_0", "curtainLightE2"),
        ("wifibleSymphonyCurtainLight",),
    ),
    173: SurplifeProduct(
        173,
        "hc3",
        ("common", "common1_0", "common2_0", "curtainLightE2"),
        ("wifibleSymphonyCurtainLight",),
    ),
    174: SurplifeProduct(
        174,
        "hc3",
        ("common", "common1_0", "common2_0", "curtainLightE2"),
        ("wifibleSymphonyCurtainLight",),
    ),
    175: SurplifeProduct(
        175, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    178: SurplifeProduct(
        178, "hc3", ("common", "common1_0", "common2_0"), ("wifibleEightPortScreenController",)
    ),
    179: SurplifeProduct(
        179,
        "hc3",
        ("common", "common1_0", "common2_0", "curtainLightE2"),
        ("wifibleSymphonyCurtainLight",),
    ),
    181: SurplifeProduct(
        181, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    182: SurplifeProduct(
        182, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    183: SurplifeProduct(
        183, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    184: SurplifeProduct(
        184, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    185: SurplifeProduct(185, "hc3", ("common", "common1_0", "common2_0"), ("wifibleSymphony",)),
    186: SurplifeProduct(
        186, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    187: SurplifeProduct(
        187, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    188: SurplifeProduct(
        188, "hc3", ("common", "common1_0", "common2_0", "symphonyE1"), ("wifibleSymphony",)
    ),
    225: SurplifeProduct(225, "xdd", ("cct_xdd", "common"), ("wifibleCCTDrive",)),
    226: SurplifeProduct(226, "xdd", ("common",), ("wifibleCCTDrive",)),
}


def product_for(product_id: int | None) -> SurplifeProduct | None:
    """Return the app's entry for a product ID, if it has one."""
    if product_id is None:
        return None
    return PRODUCTS.get(product_id)


def is_symphony_e1_product(product_id: int | None) -> bool:
    """True for products that share the verified lamp's E1 segment protocol."""
    product = product_for(product_id)
    return (
        product is not None
        and product.supports_segments
        and product_id not in _REFERENCE_EXTENDED_IDS
    )

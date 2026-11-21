from __future__ import annotations

import pytest

from custom_components.surplife_ble.core.presets import (
    PRESET_NAMES,
    gradient,
    matching_preset,
    preset_colors,
    split,
    stripes,
)


def test_gradient_hits_both_ends_and_blends() -> None:
    colors = gradient([(0, 0, 0), (200, 100, 0)], 3)
    assert colors == [(0, 0, 0), (100, 50, 0), (200, 100, 0)]
    assert gradient([(1, 2, 3)], 2) == [(1, 2, 3), (1, 2, 3)]
    assert gradient([(0, 0, 0), (9, 9, 9)], 1) == [(0, 0, 0)]


def test_stripes_and_split() -> None:
    assert stripes([(1, 1, 1), (2, 2, 2)], 5, width=2) == [
        (1, 1, 1),
        (1, 1, 1),
        (2, 2, 2),
        (2, 2, 2),
        (1, 1, 1),
    ]
    assert split([(1, 1, 1), (2, 2, 2)], 4) == [(1, 1, 1), (1, 1, 1), (2, 2, 2), (2, 2, 2)]


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_every_preset_fills_the_lamp_with_valid_colours(name: str) -> None:
    colors = preset_colors(name, 20)
    assert len(colors) == 20
    assert all(len(c) == 3 and all(0 <= part <= 255 for part in c) for c in colors)
    assert matching_preset(colors) == name


def test_matching_preset_ignores_custom_patterns() -> None:
    assert matching_preset([(1, 2, 3)] * 20) is None
    assert matching_preset(None) is None
    with pytest.raises(ValueError):
        preset_colors("Nope", 20)

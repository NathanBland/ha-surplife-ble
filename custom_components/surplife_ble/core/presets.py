"""Named per-segment colour patterns for symphony lamps.

Segment 0 is the top of the lamp, so gradients list their colours top first.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

RGB = tuple[int, int, int]


def gradient(stops: Sequence[RGB], count: int) -> list[RGB]:
    """Blend evenly between colour stops across ``count`` segments."""
    if count <= 0:
        return []
    if len(stops) == 1 or count == 1:
        return [tuple(stops[0])] * count  # type: ignore[list-item]
    span = len(stops) - 1
    colors: list[RGB] = []
    for index in range(count):
        position = index * span / (count - 1)
        low = min(int(position), span - 1)
        fraction = position - low
        start, end = stops[low], stops[low + 1]
        colors.append(
            tuple(round(a + (b - a) * fraction) for a, b in zip(start, end, strict=True))  # type: ignore[arg-type]
        )
    return colors


def stripes(colors: Sequence[RGB], count: int, width: int = 1) -> list[RGB]:
    """Repeat ``colors`` down the lamp, each band ``width`` segments tall."""
    return [tuple(colors[(index // width) % len(colors)]) for index in range(count)]  # type: ignore[misc]


def split(colors: Sequence[RGB], count: int) -> list[RGB]:
    """Divide the lamp into equal solid blocks, one per colour."""
    return [tuple(colors[index * len(colors) // count]) for index in range(count)]  # type: ignore[misc]


PRESETS: dict[str, Callable[[int], list[RGB]]] = {
    "Rainbow": lambda n: gradient(
        [
            (255, 0, 0),
            (255, 128, 0),
            (255, 255, 0),
            (0, 255, 0),
            (0, 255, 255),
            (0, 0, 255),
            (160, 0, 255),
        ],
        n,
    ),
    "Sunset": lambda n: gradient([(80, 0, 160), (255, 0, 120), (255, 80, 0), (255, 180, 0)], n),
    "Ocean": lambda n: gradient([(0, 20, 120), (0, 80, 255), (0, 200, 255), (0, 255, 180)], n),
    "Forest": lambda n: gradient([(0, 80, 10), (0, 200, 40), (140, 255, 0)], n),
    "Fire": lambda n: gradient([(255, 0, 0), (255, 60, 0), (255, 140, 0), (255, 220, 40)], n),
    "Aurora": lambda n: gradient([(0, 255, 120), (0, 160, 200), (120, 0, 255), (255, 0, 160)], n),
    "Candy cane": lambda n: stripes([(255, 0, 0), (255, 255, 255)], n, width=2),
    "Red / blue": lambda n: split([(255, 0, 0), (0, 0, 255)], n),
}

PRESET_NAMES: list[str] = list(PRESETS)


def preset_colors(name: str, count: int) -> list[RGB]:
    """Colours for preset ``name`` over ``count`` segments."""
    try:
        return PRESETS[name](count)
    except KeyError:
        raise ValueError(f"unknown preset {name!r}") from None


def matching_preset(colors: Sequence[RGB] | None) -> str | None:
    """The preset these segment colours were painted from, if any."""
    if not colors:
        return None
    current = [tuple(c) for c in colors]
    for name in PRESET_NAMES:
        if preset_colors(name, len(current)) == current:
            return name
    return None

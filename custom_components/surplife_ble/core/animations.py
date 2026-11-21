"""Built-in symphony animations from the Surplife app's custom mode list.

Model numbers come from ``getSymphonyCustomModeList`` in app 1.6.5. Models
1-22 appear in every product's list. 69 and 75-77 come from a newer list, and
it is not yet confirmed that every lamp accepts them.
"""

from __future__ import annotations

from dataclasses import dataclass

RGB = tuple[int, int, int]

DIRECTIONS: dict[str, int] = {
    "right": 1,
    "left": 2,
    "open": 3,
    "close": 4,
}

DEFAULT_COLORS: tuple[RGB, ...] = (
    (255, 0, 0),
    (255, 128, 0),
    (255, 255, 0),
    (0, 255, 0),
    (0, 255, 255),
    (0, 0, 255),
    (160, 0, 255),
)

DEFAULT_SPEED = 50


@dataclass(frozen=True)
class Animation:
    """One built-in animation and the app's defaults for it."""

    name: str
    model: int
    model_expand: int = 0
    density: int = 25


ANIMATIONS: tuple[Animation, ...] = (
    Animation("Breathe", 9),
    Animation("Alternate breath", 69, density=40),
    Animation("Async breathe", 75, density=40),
    Animation("Cycle", 8),
    Animation("Sync gradient", 77, density=40),
    Animation("Async gradient", 76, density=40),
    Animation("Rainbow bridge", 21),
    Animation("Gradient overlay", 22),
    Animation("Wave", 1),
    Animation("Meteor", 2),
    Animation("Gradient meteor", 18),
    Animation("Comet", 17),
    Animation("Streamer", 3),
    Animation("Running water", 5),
    Animation("Chase", 6),
    Animation("Horse racing", 7),
    Animation("Building blocks", 4),
    Animation("Faster light", 20),
    Animation("Volcano", 19),
    Animation("Collision", 15),
    Animation("Fireworks", 16),
    Animation("Jump", 10),
    Animation("Sharp flash", 11, model_expand=1),
    Animation("Twinkling stars", 12, model_expand=1),
    Animation("Stars wink", 13, model_expand=1),
    Animation("Warning", 14),
)


@dataclass(frozen=True)
class AnimationCommand:
    """The parameters of the last E1 01 animation sent to a lamp."""

    model: int
    colors: tuple[RGB, ...]
    background: RGB = (0, 0, 0)
    brightness: int = 80
    speed: int = DEFAULT_SPEED
    density: int = 25
    direction: int = 1
    model_expand: int = 0
    name: str | None = None


ANIMATIONS_BY_NAME: dict[str, Animation] = {animation.name: animation for animation in ANIMATIONS}
ANIMATION_NAMES: list[str] = [animation.name for animation in ANIMATIONS]


def animation_named(name: str) -> Animation:
    """The animation called ``name``."""
    try:
        return ANIMATIONS_BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown animation {name!r}") from None


def animation_for_model(model: int | None) -> Animation | None:
    """The built-in animation with this model number, if it is one we list."""
    return next((animation for animation in ANIMATIONS if animation.model == model), None)

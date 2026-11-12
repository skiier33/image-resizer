"""Shared type aliases and constants for shape masks and layout."""

from typing import Literal


MaskShape = Literal[
    "none",
    "circle",
    "rounded",
    "heart",
    "star",
    "hexagon",
    "diamond",
    "octagon",
    "triangle",
    "pentagon",
    "cross",
    "arrow",
    "edge_wave",
    "edge_zigzag",
    "edge_scallop",
    "edge_arc",
    "dollar",
    "frame",
]
EdgeSide = Literal["top", "bottom", "left", "right"]
EDGE_SHAPES: tuple[str, ...] = ("edge_wave", "edge_zigzag", "edge_scallop", "edge_arc")
# Depth of an edge cut, as a fraction of the box's size across the cut.
EDGE_DEPTH = 0.18


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value to [low, high]."""
    return max(low, min(high, value))

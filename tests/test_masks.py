"""Tests for board_core.masks: shape masks and their edge cuts."""

import pytest
from PIL import Image

from board_core.masks import apply_shape_mask, create_shape_mask

ALL_SHAPES = [
    "none", "circle", "rounded", "heart", "star", "hexagon", "diamond", "octagon",
    "triangle", "pentagon", "cross", "arrow", "edge_wave", "edge_zigzag",
    "edge_scallop", "edge_arc", "dollar", "frame",
]
EDGE_SHAPES = ["edge_wave", "edge_zigzag", "edge_scallop", "edge_arc"]


@pytest.mark.parametrize("shape", ALL_SHAPES)
def test_every_shape_gives_an_l_mask_of_the_requested_size(shape):
    mask = create_shape_mask(90, 60, shape)
    assert mask.mode == "L"
    assert mask.size == (90, 60)


def test_none_is_a_full_rectangle():
    mask = create_shape_mask(40, 30, "none")
    assert mask.getextrema() == (255, 255)


def test_circle_shows_centre_and_hides_corners():
    mask = create_shape_mask(100, 100, "circle")
    assert mask.getpixel((50, 50)) == 255
    assert mask.getpixel((0, 0)) == 0
    assert mask.getpixel((99, 99)) == 0


def test_frame_hides_the_centre_and_shows_the_border():
    mask = create_shape_mask(100, 100, "frame")
    assert mask.getpixel((50, 50)) == 0
    assert mask.getpixel((0, 50)) == 255


@pytest.mark.parametrize(
    "shape",
    [
        "edge_wave",
        "edge_zigzag",
        pytest.param(
            "edge_scallop",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Known issue: the last scallop bump clips the bottom-right corner pixel by a fraction of a pixel",
            ),
        ),
        "edge_arc",
    ],
)
def test_edge_cuts_never_reach_the_bottom_corners(shape):
    # The edge profiles are zero at the corners, so a bottom cut must leave them solid.
    mask = create_shape_mask(120, 80, shape, sides=("bottom",))
    assert mask.getpixel((0, 79)) == 255
    assert mask.getpixel((119, 79)) == 255


@pytest.mark.parametrize("shape", EDGE_SHAPES)
def test_edge_cut_removes_some_pixels_on_its_side(shape):
    mask = create_shape_mask(120, 80, shape, sides=("bottom",))
    assert mask.getextrema()[0] == 0


def test_multiple_edge_sides_only_remove_more_pixels():
    one = create_shape_mask(120, 80, "edge_wave", sides=("bottom",))
    two = create_shape_mask(120, 80, "edge_wave", sides=("bottom", "top"))
    assert sum(two.histogram()[:128]) >= sum(one.histogram()[:128])


def test_apply_shape_mask_multiplies_alpha_and_keeps_colour():
    image = Image.new("RGB", (20, 20), (200, 10, 30))
    mask = create_shape_mask(20, 20, "circle")
    out = apply_shape_mask(image, mask)
    assert out.mode == "RGBA"
    assert out.getpixel((10, 10))[:3] == (200, 10, 30)
    assert out.getpixel((10, 10))[3] == 255
    assert out.getpixel((0, 0))[3] == 0


@pytest.mark.xfail(strict=True, raises=ValueError, reason="Known bug: a 1px-tall circle is inset past its own bounds")
def test_one_pixel_tall_circle_does_not_crash():
    create_shape_mask(115, 1, "circle")

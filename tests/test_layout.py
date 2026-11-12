"""Tests for board_core.layout: placing boxes, arranging images, and collages."""

import math

import pytest

from board_core.layout import (
    _tile_guillotine,
    aspect_locked_corner_rect,
    compute_arrangement,
    compute_collage,
    compute_shelf_layout,
)


def _overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _inside(rect, page_w, page_h, margin=0.0):
    x, y, w, h = rect
    return x >= margin - 1e-6 and y >= margin - 1e-6 and x + w <= page_w - margin + 1e-6 and y + h <= page_h - margin + 1e-6


# --- compute_shelf_layout ---------------------------------------------------

def test_shelf_layout_fits_small_boxes_on_the_page():
    sizes = [(100, 50), (80, 80), (120, 40)]
    positions, fits = compute_shelf_layout(sizes, 800, 600, margin=20, gap=10)
    assert fits is True
    for (x, y), (w, h) in zip(positions, sizes):
        assert _inside((x, y, w, h), 800, 600, margin=20)


def test_shelf_layout_reports_when_the_block_is_too_tall():
    # Two rows of 500px-tall boxes cannot fit the 380px of height left after the margins.
    sizes = [(100, 500)] * 4
    _, fits = compute_shelf_layout(sizes, 800, 400, margin=10, gap=10)
    assert fits is False


def test_shelf_layout_wraps_rows_and_keeps_boxes_apart():
    sizes = [(300, 100)] * 5
    positions, _ = compute_shelf_layout(sizes, 700, 1000, margin=10, gap=10)
    rects = [(x, y, w, h) for (x, y), (w, h) in zip(positions, sizes)]
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            assert not _overlaps(rects[i], rects[j])


# --- compute_arrangement ----------------------------------------------------

@pytest.mark.parametrize("aspects", [[1.0], [1.5, 0.75, 2.0], [0.5] * 7])
def test_arrangement_returns_one_rect_per_image_inside_the_margins(aspects):
    rects = compute_arrangement(aspects, 1000, 800, margin=30, gap=10)
    assert len(rects) == len(aspects)
    for rect in rects:
        assert _inside(rect, 1000, 800, margin=30)


def test_arrangement_keeps_each_image_at_its_aspect_ratio():
    aspects = [1.5, 0.6, 1.0, 2.2]
    for (_, _, w, h), aspect in zip(compute_arrangement(aspects, 900, 700, 20, 15), aspects):
        assert math.isclose(w / h, aspect, rel_tol=1e-9)


def test_arrangement_empty_input_gives_no_rects():
    assert compute_arrangement([], 500, 500, 10, 10) == []


def test_arrangement_weights_make_heavier_images_larger():
    rects = compute_arrangement([1.0, 1.0], 1000, 500, 0, 0, weights=[4.0, 1.0])
    assert rects[0][2] > rects[1][2]


# --- _tile_guillotine and compute_collage -----------------------------------

def test_guillotine_tiles_exactly_fill_the_box_in_proportion_to_weight():
    out = [None, None, None]
    weights = [1.0, 2.0, 1.0]
    _tile_guillotine([0, 1, 2], 0.0, 0.0, 40.0, 30.0, out, weights)
    total_area = sum(w * h for _, _, w, h in out)
    assert math.isclose(total_area, 40.0 * 30.0, rel_tol=1e-9)
    assert math.isclose(out[1][2] * out[1][3], 2 * out[0][2] * out[0][3], rel_tol=1e-9)


def test_collage_returns_one_rect_per_image_and_sane_stats():
    aspects = [1.0, 1.5, 0.7, 1.2]
    rects, coverage, overlap = compute_collage(aspects, 800, 600, "rounded", max_overlap=0.5)
    assert len(rects) == len(aspects)
    assert 0.0 < coverage <= 1.0
    assert 0.0 <= overlap <= 1.0
    for rect in rects:
        assert _inside(rect, 800 + 1e-6, 600 + 1e-6)


def test_collage_with_no_images_is_empty():
    assert compute_collage([], 800, 600, "rounded") == ([], 0.0, 0.0)


def test_collage_with_one_image_covers_most_of_the_page():
    _, coverage, overlap = compute_collage([1.0], 500, 500, "none")
    assert coverage > 0.9
    assert overlap == 0.0


# --- aspect_locked_corner_rect ----------------------------------------------

@pytest.mark.parametrize("mode", ["se", "nw", "ne", "sw"])
def test_corner_drag_keeps_the_requested_aspect(mode):
    x, y, x2, y2 = aspect_locked_corner_rect(mode, 10, 10, 210, 60, aspect=2.0, min_size=5)
    assert math.isclose((x2 - x) / (y2 - y), 2.0, rel_tol=1e-9)


def test_corner_drag_respects_minimum_size():
    x1, y1, x2, y2 = aspect_locked_corner_rect("se", 0, 0, 1, 1, aspect=1.0, min_size=20)
    assert x2 - x1 >= 20 and y2 - y1 >= 20


def test_corner_drag_with_unknown_mode_returns_the_box_unchanged():
    assert aspect_locked_corner_rect("x", 1, 2, 30, 40, aspect=1.0, min_size=1) == (1, 2, 30, 40)

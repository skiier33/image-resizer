"""Layout: place boxes on a page, tile collages, and constrain corner drags."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Union

from board_core.common import MaskShape
from board_core.masks import create_shape_mask


def compute_shelf_layout(
    sizes: list[tuple[float, float]],
    page_width: float,
    page_height: float,
    margin: float,
    gap: float,
) -> tuple[list[tuple[float, float]], bool]:
    """Place boxes of fixed size in left-to-right rows that wrap at the margins.

    Returns (top-left positions, fits). Each row and the whole block are centred; when
    the block is taller than the page it starts at the top margin and fits is False.
    """
    avail_w = page_width - 2 * margin
    avail_h = page_height - 2 * margin
    rows: list[list[int]] = [[]]
    row_w = 0.0
    for i, (w, _h) in enumerate(sizes):
        needed = w if not rows[-1] else row_w + gap + w
        if rows[-1] and needed > avail_w:
            rows.append([])
            needed = w
        rows[-1].append(i)
        row_w = needed
    row_widths = [sum(sizes[i][0] for i in r) + gap * (len(r) - 1) for r in rows]
    row_heights = [max(sizes[i][1] for i in r) for r in rows]
    total_h = sum(row_heights) + gap * (len(rows) - 1)
    fits = total_h <= avail_h and all(rw <= avail_w for rw in row_widths)
    y = margin + max(0.0, (avail_h - total_h) / 2)
    positions: list[tuple[float, float]] = [(0.0, 0.0)] * len(sizes)
    for r, row in enumerate(rows):
        x = margin + max(0.0, (avail_w - row_widths[r]) / 2)
        for i in row:
            w, h = sizes[i]
            positions[i] = (x, y + (row_heights[r] - h) / 2)
            x += w + gap
        y += row_heights[r] + gap
    return positions, fits


def compute_arrangement(
    aspects: list[float],
    page_width: float,
    page_height: float,
    margin: float,
    gap: float,
    weights: Optional[list[float]] = None,
) -> list[tuple[float, float, float, float]]:
    """Return (x, y, w, h) cell footprints in page coordinates, one per aspect ratio.

    Tries every grid column count, sizes uniform cells to fit the page, and keeps the
    layout where the images cover the most area. Rows and the whole block are centred.
    """
    count = len(aspects)
    avail_w = page_width - 2 * margin
    avail_h = page_height - 2 * margin
    if count == 0 or avail_w <= 0 or avail_h <= 0:
        return []

    best_cols = 0
    best_area = -1.0
    for cols in range(1, count + 1):
        rows = math.ceil(count / cols)
        cell_w = (avail_w - (cols - 1) * gap) / cols
        cell_h = (avail_h - (rows - 1) * gap) / rows
        if cell_w <= 0 or cell_h <= 0:
            continue
        area = 0.0
        for aspect in aspects:
            w = min(cell_w, cell_h * aspect)
            area += w * (w / aspect)
        if area > best_area:
            best_area, best_cols = area, cols
    if best_cols == 0:
        return []

    cols = best_cols
    rows = math.ceil(count / cols)
    cell_w = (avail_w - (cols - 1) * gap) / cols
    cell_h = (avail_h - (rows - 1) * gap) / rows

    sizes: list[tuple[float, float]] = []
    for aspect in aspects:
        w = min(cell_w, cell_h * aspect)
        sizes.append((w, w / aspect))
    if weights is not None and len(weights) == count:
        # Area follows weight. The heaviest image keeps the full cell and the others shrink
        # by the square root of their weight ratio, so the size ratios stay exact.
        top_weight = max(1e-6, max(weights))
        for i, aspect in enumerate(aspects):
            factor = math.sqrt(max(1e-6, weights[i]) / top_weight)
            w = min(sizes[i][0] * factor, cell_w, cell_h * aspect)
            sizes[i] = (w, w / aspect)

    row_indices = [list(range(start, min(start + cols, count))) for start in range(0, count, cols)]
    row_dims: list[tuple[float, float]] = []
    for idxs in row_indices:
        row_w = sum(sizes[i][0] for i in idxs) + (len(idxs) - 1) * gap
        row_h = max(sizes[i][1] for i in idxs)
        row_dims.append((row_w, row_h))
    total_h = sum(rh for _, rh in row_dims) + (len(row_dims) - 1) * gap

    rects: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 0.0)] * count
    y = margin + (avail_h - total_h) / 2
    for idxs, (row_w, row_h) in zip(row_indices, row_dims):
        x = margin + (avail_w - row_w) / 2
        for i in idxs:
            w, h = sizes[i]
            rects[i] = (x, y + (row_h - h) / 2, w, h)
            x += w + gap
        y += row_h + gap
    return rects


COLLAGE_GRID_CELLS = 240  # coverage grid resolution along the longer page side
COLLAGE_MAX_GROW = 2.2  # largest factor an image may grow past its tile to fill masked gaps
COLLAGE_GROW_STEPS = 12
COLLAGE_OVERLAP_WEIGHT = 0.5  # score cost of covering a cell that is already covered
COLLAGE_PASSES = 2
COLLAGE_DEFAULT_OVERLAP_PCT = 10
COLLAGE_DEFAULT_CORNER_PCT = 20  # corner radius as a percent of the cell's shorter side
COLLAGE_MAX_CORNER_PCT = 50
COLLAGE_MAX_STROKE = 50


def _tile_guillotine(
    indices: list[int],
    x: float,
    y: float,
    w: float,
    h: float,
    out: list[tuple[float, float, float, float]],
    weights: list[float],
) -> None:
    """Split a rectangle into one tile per index. Each tile's area is proportional to its weight.

    The split falls where the running weight is closest to half, and the longer side is cut.
    With equal weights this is an even split by count.
    """
    if len(indices) == 1:
        out[indices[0]] = (x, y, w, h)
        return
    total = sum(weights[i] for i in indices)
    split = 1
    best = None
    running = 0.0
    for k in range(1, len(indices)):
        running += weights[indices[k - 1]]
        diff = abs(running - total / 2)
        if best is None or diff < best - 1e-12:
            best, split = diff, k
    left, right = indices[:split], indices[split:]
    frac = sum(weights[i] for i in left) / total
    if w >= h:
        left_w = w * frac
        _tile_guillotine(left, x, y, left_w, h, out, weights)
        _tile_guillotine(right, x + left_w, y, w - left_w, h, out, weights)
    else:
        left_h = h * frac
        _tile_guillotine(left, x, y, w, left_h, out, weights)
        _tile_guillotine(right, x, y + left_h, w, h - left_h, out, weights)


def _shape_cells(
    shape: MaskShape,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    grid_w: int,
    grid_h: int,
    corner_fraction: float,
) -> set[int]:
    """Grid cells (index = row * grid_w + col) covered by a shape filling the box, clipped to the page."""
    ox, oy = math.floor(x0), math.floor(y0)
    mw, mh = math.ceil(x1) - ox, math.ceil(y1) - oy
    if mw < 1 or mh < 1:
        return set()
    mask = create_shape_mask(mw, mh, shape, corner_radius=int(corner_fraction * min(mw, mh)))
    pixels = mask.load()
    cells: set[int] = set()
    for row in range(max(0, oy), min(grid_h, oy + mh)):
        for col in range(max(0, ox), min(grid_w, ox + mw)):
            if pixels[col - ox, row - oy] >= 128:
                cells.add(row * grid_w + col)
    return cells


@dataclass
class CollageItemOptions:
    """Per-image choices for Make collage: resize percent (sets the image's share of the page),
    mask shape, and stroke."""

    percent: float
    shape: MaskShape
    stroke_width: int
    stroke_color: str


def compute_collage(
    aspects: list[float],
    page_width: float,
    page_height: float,
    shape: Union[MaskShape, list[MaskShape]],
    max_overlap: float = 1.0,
    corner_fraction: float = COLLAGE_DEFAULT_CORNER_PCT / 100,
    weights: Optional[list[float]] = None,
) -> tuple[list[tuple[float, float, float, float]], float, float]:
    """Lay out images to fill the page with minimal overlap.

    max_overlap caps the overlap as a fraction of covered area: growth that would take the
    overall overlap past it is skipped. Returns (rects, coverage, overlap): one (x, y, w, h)
    per aspect in page coordinates, the fraction of the page covered by visible shapes, and
    the fraction of covered cells that are covered by more than one image.
    """
    count = len(aspects)
    if count == 0 or page_width <= 0 or page_height <= 0:
        return [], 0.0, 0.0

    shapes = list(shape) if isinstance(shape, list) else [shape] * count
    scale = COLLAGE_GRID_CELLS / max(page_width, page_height)
    grid_w = max(1, round(page_width * scale))
    grid_h = max(1, round(page_height * scale))
    unit_x = page_width / grid_w
    unit_y = page_height / grid_h

    # Tiles covering the whole grid, sized by weight (equal when none given), similar aspects next to each other.
    weight_list = [max(1e-6, float(v)) for v in weights] if weights is not None else [1.0] * count
    tiles: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 0.0)] * count
    order = sorted(range(count), key=lambda i: aspects[i])
    _tile_guillotine(order, 0.0, 0.0, float(grid_w), float(grid_h), tiles, weight_list)

    def rect_for(i: int, factor: float) -> tuple[float, float, float, float]:
        tx, ty, tw, th = tiles[i]
        w, h = tw * factor, th * factor
        return tx + tw / 2 - w / 2, ty + th / 2 - h / 2, w, h

    def cells_for(i: int, factor: float) -> set[int]:
        x, y, w, h = rect_for(i, factor)
        return _shape_cells(shapes[i], x, y, x + w, y + h, grid_w, grid_h, corner_fraction)

    factors = [1.0] * count
    footprints = [cells_for(i, 1.0) for i in range(count)]
    cover = [0] * (grid_w * grid_h)
    # covered: cells with at least one image; overlapped: cells with at least two.
    covered = 0
    overlapped = 0
    for cells in footprints:
        for cell in cells:
            if cover[cell] == 0:
                covered += 1
            elif cover[cell] == 1:
                overlapped += 1
            cover[cell] += 1

    # Visit images with the most empty space in their tile first. Each one grows only when
    # the extra cells it gains outweigh the penalty for re-covering cells already visible,
    # and only while the overall overlap stays within max_overlap.
    steps = [1.0 + (COLLAGE_MAX_GROW - 1.0) * s / COLLAGE_GROW_STEPS for s in range(COLLAGE_GROW_STEPS + 1)]
    for _ in range(COLLAGE_PASSES):
        visit = sorted(range(count), key=lambda i: len(footprints[i]))
        for i in visit:
            # Take this image's footprint out of the counts before testing alternatives.
            for cell in footprints[i]:
                if cover[cell] == 1:
                    covered -= 1
                elif cover[cell] == 2:
                    overlapped -= 1
                cover[cell] -= 1
            best_score = None
            best_factor = 1.0
            best_cells: set[int] = set()
            for factor in steps:
                cand = cells_for(i, factor)
                gain = 0
                touched = 0
                newly_overlapped = 0
                for c in cand:
                    if cover[c] == 0:
                        gain += 1
                    else:
                        touched += 1
                        if cover[c] == 1:
                            newly_overlapped += 1
                new_covered = covered + gain
                new_overlapped = overlapped + newly_overlapped
                # The unchanged tile (factor 1.0) is always allowed, so the cap never forces a worse layout.
                if factor > 1.0 and new_covered and new_overlapped > max_overlap * new_covered:
                    continue
                score = gain - COLLAGE_OVERLAP_WEIGHT * touched
                if best_score is None or score > best_score + 1e-9:
                    best_score, best_factor, best_cells = score, factor, cand
            factors[i] = best_factor
            footprints[i] = best_cells
            for cell in best_cells:
                if cover[cell] == 0:
                    covered += 1
                elif cover[cell] == 1:
                    overlapped += 1
                cover[cell] += 1

    covered = sum(1 for c in cover if c > 0)
    overlapped = sum(1 for c in cover if c > 1)
    total = grid_w * grid_h
    coverage = covered / total
    overlap = overlapped / covered if covered else 0.0

    rects: list[tuple[float, float, float, float]] = []
    for i in range(count):
        x, y, w, h = rect_for(i, factors[i])
        rects.append((x * unit_x, y * unit_y, w * unit_x, h * unit_y))
    return rects, coverage, overlap


def aspect_locked_corner_rect(
    mode: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    aspect: float,
    min_size: float,
) -> tuple[float, float, float, float]:
    """Adjust a corner-drag rectangle so its width / height stays at aspect.

    The corner opposite the dragged handle stays put. The larger of the two sizes wins,
    so the box follows whichever direction the cursor moved further.
    """
    w = max(x2 - x1, (y2 - y1) * aspect, min_size, min_size * aspect)
    h = w / aspect
    if mode == "se":
        return x1, y1, x1 + w, y1 + h
    if mode == "nw":
        return x2 - w, y2 - h, x2, y2
    if mode == "ne":
        return x1, y2 - h, x1 + w, y2
    if mode == "sw":
        return x2 - w, y1, x2, y1 + h
    return x1, y1, x2, y2

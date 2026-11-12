"""Manifestation board editor: resize images, collage on a custom poster, draw, and export."""

from __future__ import annotations

import json
import math
import tkinter as tk
import uuid
from dataclasses import dataclass, field, replace
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from pathlib import Path
from typing import Callable, Literal, Optional, Union

from PIL import Image, ImageChops, ImageDraw, ImageTk

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
HANDLE_RADIUS = 6
MIN_LAYER_SIZE = 8
MIN_PAGE_SIZE = 100
MAX_PAGE_SIZE = 16_384
MIN_OUTPUT_SIZE = 1
MAX_OUTPUT_SIZE = 32_768
# Percent of the original image size for each importance preset in the asset panel.
# Built-in percents of the original size; users can change them in Settings > Resize presets.
DEFAULT_IMPORTANCE_PERCENT: dict[str, int] = {"Low": 25, "Medium": 50, "High": 100}
IMPORTANCE_PERCENT_MIN = 1
IMPORTANCE_PERCENT_MAX = 400
UNDO_LIMIT = 50
CLIPBOARD_PASTE_OFFSET = 20.0
ARRANGE_DEFAULT_MARGIN = 40
ARRANGE_DEFAULT_GAP = 20
ARRANGE_MAX_MARGIN = 2_000
ARRANGE_MAX_GAP = 2_000
MASK_OUTLINE_COLOR = "#e91e63"
# Tk event.state modifier bits.
SHIFT_MASK = 0x0001
CTRL_MASK = 0x0004

# UI chrome colours only. Document content (page, layer and stroke/fill colours) is not themed.
THEME_PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "root": "#ececec",
        "fg": "#000000",
        "field": "#ffffff",
        "button": "#e1e1e1",
        "active": "#e5f1fb",
        "select": "#0078d7",
        "select_fg": "#ffffff",
        "border": "#adadad",
        "hint": "#555555",
        "disabled": "#6d6d6d",
    },
    "dark": {
        "root": "#1e1e1e",
        "fg": "#e6e6e6",
        "field": "#2b2b2b",
        "button": "#3a3a3a",
        "active": "#4a4a4a",
        "select": "#4a9eff",
        "select_fg": "#101010",
        "border": "#555555",
        "hint": "#9a9a9a",
        "disabled": "#7a7a7a",
    },
}

SETTINGS_PATH = Path.home() / ".image_resizer_settings.json"


def load_settings() -> dict[str, object]:
    """Return saved app settings, or an empty dict if none can be read."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_importance_percent() -> dict[str, int]:
    """Return the Low, Medium and High percents, using saved values where they are valid."""
    result = dict(DEFAULT_IMPORTANCE_PERCENT)
    saved = load_settings().get("importance_percent")
    if isinstance(saved, dict):
        for level in result:
            value = saved.get(level)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and IMPORTANCE_PERCENT_MIN <= value <= IMPORTANCE_PERCENT_MAX
            ):
                result[level] = value
    return result


def update_settings(**values: object) -> None:
    """Merge values into the saved settings file. Failures are ignored."""
    settings = load_settings()
    settings.update(values)
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def _style_dark_ttk(style: ttk.Style, p: dict[str, str]) -> None:
    """Configure ttk widgets for dark mode. Requires a theme that honours colour options."""
    style.configure(
        ".",
        background=p["root"],
        foreground=p["fg"],
        fieldbackground=p["field"],
        bordercolor=p["border"],
        darkcolor=p["border"],
        lightcolor=p["border"],
        troughcolor=p["root"],
        selectbackground=p["select"],
        selectforeground=p["select_fg"],
        insertcolor=p["fg"],
        arrowcolor=p["fg"],
    )
    style.configure("TButton", background=p["button"], foreground=p["fg"])
    style.map(
        "TButton",
        background=[("pressed", p["active"]), ("active", p["active"]), ("disabled", p["root"])],
        foreground=[("disabled", p["disabled"])],
    )
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.map(
            name,
            fieldbackground=[("readonly", p["root"]), ("disabled", p["root"])],
            foreground=[("disabled", p["disabled"])],
        )
    style.configure("TNotebook", background=p["root"], bordercolor=p["border"])
    style.configure("TNotebook.Tab", background=p["button"], foreground=p["fg"])
    style.map(
        "TNotebook.Tab",
        background=[("selected", p["root"]), ("active", p["active"])],
        foreground=[("selected", p["fg"])],
    )
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=p["root"], foreground=p["fg"], indicatorbackground=p["field"])
        style.map(name, background=[("active", p["root"])])
    style.configure("TScrollbar", background=p["button"], troughcolor=p["root"], arrowcolor=p["fg"])
    style.configure("TLabelframe", background=p["root"], bordercolor=p["border"])
    style.configure("TLabelframe.Label", background=p["root"], foreground=p["fg"])

ToolName = Literal["select", "line", "rect", "ellipse", "fill", "crop"]
LayerKind = Literal["image", "mask", "line", "rect", "ellipse"]
AlignMode = Literal["left", "right", "top", "bottom", "center_h", "center_v", "center"]
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
]
EdgeSide = Literal["top", "bottom", "left", "right"]
EDGE_SHAPES: tuple[str, ...] = ("edge_wave", "edge_zigzag", "edge_scallop", "edge_arc")
EDGE_SIDE_LABELS: dict[EdgeSide, str] = {"top": "Top", "bottom": "Bottom", "left": "Left", "right": "Right"}
# Depth of an edge cut, as a fraction of the box's size across the cut.
EDGE_DEPTH = 0.18

MASK_SHAPE_LABELS: dict[MaskShape, str] = {
    "none": "None (rectangle)",
    "circle": "Circle",
    "rounded": "Rounded rectangle",
    "heart": "Heart",
    "star": "Star",
    "hexagon": "Hexagon",
    "diamond": "Diamond",
    "octagon": "Octagon",
    "triangle": "Triangle",
    "pentagon": "Pentagon",
    "cross": "Cross",
    "arrow": "Arrow",
    "edge_wave": "Wave edge",
    "edge_zigzag": "Zigzag edge",
    "edge_scallop": "Scallop edge",
    "edge_arc": "Arc edge",
}


def is_image_path(path: Path) -> bool:
    """Return True if path has a supported image extension."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value to [low, high]."""
    return max(low, min(high, value))


def new_id() -> str:
    """Return a unique layer identifier."""
    return uuid.uuid4().hex[:12]


def _star_vertices(cx: float, cy: float, outer_r: float, points: int = 5) -> list[tuple[float, float]]:
    """Return vertices for a regular star centered at (cx, cy)."""
    inner_r = outer_r * 0.6
    verts: list[tuple[float, float]] = []
    for i in range(points * 2):
        angle = math.pi / 2 + i * math.pi / points
        radius = outer_r if i % 2 == 0 else inner_r
        verts.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    return verts


def _regular_polygon_vertices(
    cx: float,
    cy: float,
    radius: float,
    sides: int,
    rotation: float = -math.pi / 2,
) -> list[tuple[float, float]]:
    """Return vertices for a regular polygon."""
    verts: list[tuple[float, float]] = []
    for i in range(sides):
        angle = rotation + i * 2 * math.pi / sides
        verts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return verts


def _heart_vertices(width: int, height: int) -> list[tuple[float, float]]:
    """Return vertices tracing a heart stretched to fill the given box edge to edge."""
    raw: list[tuple[float, float]] = []
    for i in range(72):
        t = 2 * math.pi * i / 72
        hx = 16 * math.sin(t) ** 3
        hy = -(
            13 * math.cos(t)
            - 5 * math.cos(2 * t)
            - 2 * math.cos(3 * t)
            - math.cos(4 * t)
        )
        raw.append((hx, hy))
    # Normalise to the outline's real extents so the heart uses the whole box.
    min_x = min(x for x, _ in raw)
    max_x = max(x for x, _ in raw)
    min_y = min(y for _, y in raw)
    max_y = max(y for _, y in raw)
    inset = 1.0
    span_x = max(1.0, width - 2 * inset)
    span_y = max(1.0, height - 2 * inset)
    return [
        (
            inset + (x - min_x) / (max_x - min_x) * span_x,
            inset + (y - min_y) / (max_y - min_y) * span_y,
        )
        for x, y in raw
    ]


def _edge_profile(kind: str, t: float) -> float:
    """Cut depth, 0..1, at position t (0..1) along an edge shape's side."""
    if kind == "edge_wave":
        return 0.5 - 0.5 * math.cos(4 * math.pi * t)
    if kind == "edge_zigzag":
        # Triangle wave: three teeth, zero at both corners so the cut never reaches them.
        return 1.0 - abs(2 * ((3 * t) % 1.0) - 1.0)
    if kind == "edge_scallop":
        # Three rounded bumps. Sine bumps meet the corners gently, so the cut stays off them.
        return math.sin(math.pi * ((3 * t) % 1.0))
    if kind == "edge_arc":
        return math.sin(math.pi * t)
    return 0.0


def _edge_mask(width: int, height: int, kind: str, side: EdgeSide, flipped: bool) -> Image.Image:
    """Rectangle whose one side follows an edge profile. The profile is drawn on the bottom
    side and then rotated or mirrored onto the requested side."""
    if side in ("left", "right"):
        cw, ch = height, width
    else:
        cw, ch = width, height
    depth = EDGE_DEPTH * ch
    steps = max(2, cw)
    points = [(0.0, 0.0), (float(cw), 0.0)]
    for i in range(steps, -1, -1):
        t = i / steps
        f = _edge_profile(kind, t)
        if flipped:
            # Invert the bumps but taper to zero at the corners, so flipping never cuts them.
            f = math.sin(math.pi * t) * (1.0 - f)
        points.append((cw * t, ch - depth * f))
    canonical = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(canonical).polygon(points, fill=255)
    if side == "top":
        return canonical.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if side == "right":
        return canonical.transpose(Image.Transpose.ROTATE_90)
    if side == "left":
        return canonical.transpose(Image.Transpose.ROTATE_270)
    return canonical


def create_shape_mask(
    width: int,
    height: int,
    shape: MaskShape,
    corner_radius: int = 32,
    side: EdgeSide = "bottom",
    flipped: bool = False,
) -> Image.Image:
    """Build an L-mode mask (255 = visible) for the given shape."""
    width = max(1, width)
    height = max(1, height)
    if shape in EDGE_SHAPES:
        return _edge_mask(width, height, shape, side, flipped)
    mask = Image.new("L", (width, height), 0)
    if shape == "none":
        return Image.new("L", (width, height), 255)

    draw = ImageDraw.Draw(mask)
    inset = 1
    x1, y1 = inset, inset
    x2, y2 = width - inset, height - inset
    cx, cy = width / 2, height / 2

    if shape == "circle":
        draw.ellipse((x1, y1, x2, y2), fill=255)
    elif shape == "rounded":
        radius = int(clamp(corner_radius, 0, min(width, height) // 2))
        # Full bounds, so a 0 radius gives a square mask with no transparent edge.
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    elif shape == "heart":
        draw.polygon(_heart_vertices(width, height), fill=255)
    elif shape == "star":
        outer_r = min(width, height) / 2 - inset
        draw.polygon(_star_vertices(cx, cy, outer_r), fill=255)
    elif shape == "hexagon":
        # Flat top and bottom, sized so the hexagon fills the box width or height, whichever is tighter.
        outer_r = min((width - 2 * inset) / 2, (height - 2 * inset) / math.sqrt(3))
        draw.polygon(_regular_polygon_vertices(cx, cy, outer_r, 6, rotation=0.0), fill=255)
    elif shape == "octagon":
        outer_r = min(width, height) / 2 - inset
        draw.polygon(_regular_polygon_vertices(cx, cy, outer_r, 8, rotation=-math.pi / 8), fill=255)
    elif shape == "diamond":
        draw.polygon(
            [(cx, y1), (x2, cy), (cx, y2), (x1, cy)],
            fill=255,
        )
    elif shape == "triangle":
        draw.polygon([(cx, y1), (x2, y2), (x1, y2)], fill=255)
    elif shape == "pentagon":
        # Point up, sized to fit the tighter side of the box.
        outer_r = min(width, height) / 2 - inset
        draw.polygon(_regular_polygon_vertices(cx, cy, outer_r, 5, rotation=-math.pi / 2), fill=255)
    elif shape == "cross":
        # Plus sign with arms a third of the box wide.
        arm = min(width, height) / 3
        left, right = cx - arm / 2, cx + arm / 2
        top, bottom = cy - arm / 2, cy + arm / 2
        draw.polygon(
            [
                (left, y1), (right, y1), (right, top), (x2, top),
                (x2, bottom), (right, bottom), (right, y2), (left, y2),
                (left, bottom), (x1, bottom), (x1, top), (left, top),
            ],
            fill=255,
        )
    elif shape == "arrow":
        # Block arrow pointing right: a shaft over the left part, a head on the right.
        shaft = height / 4
        head_x = x2 - width * 0.45
        draw.polygon(
            [
                (x1, cy - shaft), (head_x, cy - shaft), (head_x, y1), (x2, cy),
                (head_x, y2), (head_x, cy + shaft), (x1, cy + shaft),
            ],
            fill=255,
        )
    return mask


def apply_shape_mask(image: Image.Image, mask: Image.Image) -> Image.Image:
    """Multiply image alpha by a shape mask."""
    rgba = image.convert("RGBA")
    alpha = rgba.split()[3]
    rgba.putalpha(ImageChops.multiply(alpha, mask))
    return rgba


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


def compute_collage(
    aspects: list[float],
    page_width: float,
    page_height: float,
    shape: MaskShape,
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
        return _shape_cells(shape, x, y, x + w, y + h, grid_w, grid_h, corner_fraction)

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


def record_resize_percent(layer: ImageLayer) -> None:
    """Store the layer's displayed size as a percent of its original (pre-collage) pixels."""
    base = layer.collage_source if layer.collage_source is not None else layer.source
    layer.resize_percent = layer.width / max(1, base.width) * 100


def _collage_base(layer: ImageLayer) -> Image.Image:
    """The pixels a collage run should start from: the pre-collage source if there is one."""
    return layer.collage_source if layer.collage_source is not None else layer.source


def center_crop_to_aspect(image: Image.Image, aspect: float) -> Image.Image:
    """Crop the middle of an image so its width/height ratio equals aspect, without scaling."""
    w, h = image.size
    if w / h > aspect:
        new_w = max(1, round(h * aspect))
        left = (w - new_w) // 2
        return image.crop((left, 0, left + new_w, h))
    new_h = max(1, round(w / aspect))
    top = (h - new_h) // 2
    return image.crop((0, top, w, top + new_h))


def _sliding_max(img: Image.Image, length: int, horizontal: bool, lead: int) -> Image.Image:
    """Grayscale max over a window of `length` pixels along one axis, starting `lead` px before each pixel.

    Doubling shifts make this O(log length) passes; pixels shifted off the canvas count as 0.
    """

    def moved(im: Image.Image, k: int) -> Image.Image:
        out = Image.new("L", im.size, 0)
        out.paste(im, (k, 0) if horizontal else (0, k))
        return out

    cur = moved(img, -lead)
    span = 1
    while span * 2 <= length:
        cur = ImageChops.lighter(cur, moved(cur, span))
        span *= 2
    if span < length:
        cur = ImageChops.lighter(cur, moved(cur, length - span))
    return cur


def dilate_alpha(alpha: Image.Image, radius: int) -> Image.Image:
    """Grow an L-mode alpha outward by radius px (square neighbourhood)."""
    if radius <= 0:
        return alpha
    length = 2 * radius + 1
    out = _sliding_max(alpha, length, True, radius)
    return _sliding_max(out, length, False, radius)


def compose_image_layer(
    layer: ImageLayer,
    masks: Optional[list["MaskLayer"]] = None,
) -> tuple[Image.Image, int, int]:
    """Rasterize an image layer to RGBA, clipped by its masks, and return paste position (ox, oy).

    Border modes:
    - stroke before mask (default): a rectangle around the image, then cut by the masks.
    - stroke after mask: an outline around the visible, masked shape.
    """
    resized = layer.source.convert("RGBA").resize(
        (max(1, int(layer.width)), max(1, int(layer.height))),
        Image.Resampling.LANCZOS,
    )
    if layer.opacity < 1.0:
        alpha = resized.split()[3]
        alpha = alpha.point(lambda p: int(p * layer.opacity))
        resized.putalpha(alpha)

    mask_list = masks or []
    x, y = int(layer.x), int(layer.y)
    bw = max(0, layer.border_width)
    if bw == 0:
        return clip_to_masks(resized, x, y, mask_list), x, y

    if layer.stroke_after_mask:
        clipped = clip_to_masks(resized, x, y, mask_list)
        padded = Image.new("RGBA", (clipped.width + bw * 2, clipped.height + bw * 2), (0, 0, 0, 0))
        padded.paste(clipped, (bw, bw))
        alpha = padded.split()[3]
        ring = ImageChops.subtract(dilate_alpha(alpha, bw), alpha)
        outline = Image.new("RGBA", padded.size, layer.border_color)
        outline.putalpha(ring)
        outline.alpha_composite(padded)
        return outline, x - bw, y - bw

    bordered = Image.new(
        "RGBA",
        (resized.width + bw * 2, resized.height + bw * 2),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(bordered)
    draw.rectangle(
        (0, 0, bordered.width - 1, bordered.height - 1),
        outline=layer.border_color,
        width=bw,
    )
    bordered.paste(resized, (bw, bw), resized)
    return clip_to_masks(bordered, x - bw, y - bw, mask_list), x - bw, y - bw


def mask_alpha(
    width: int,
    height: int,
    mask: MaskLayer,
    origin_x: float,
    origin_y: float,
    scale_x: float,
    scale_y: float,
) -> Image.Image:
    """Return an L-mode alpha of size (width, height) with the mask placed in that space.

    origin is the page position of the space's top-left corner; scale converts page
    pixels to the space's pixels (1.0 when the space is the board).
    """
    mw = max(1, round(mask.width * scale_x))
    mh = max(1, round(mask.height * scale_y))
    shape_mask = create_shape_mask(
        mw,
        mh,
        mask.shape,
        round(mask.corner_radius * min(scale_x, scale_y)),
        side=mask.side,
        flipped=mask.flipped,
    )
    alpha = Image.new("L", (width, height), 0)
    alpha.paste(
        shape_mask,
        (round((mask.x - origin_x) * scale_x), round((mask.y - origin_y) * scale_y)),
    )
    return alpha


def clip_to_masks(
    image: Image.Image,
    ox: int,
    oy: int,
    masks: list[MaskLayer],
) -> Image.Image:
    """Multiply the image's alpha by each mask that covers it (image sits at page offset ox, oy)."""
    result = image
    for mask in masks:
        alpha = mask_alpha(result.width, result.height, mask, ox, oy, 1.0, 1.0)
        result = result.copy()
        result.putalpha(ImageChops.multiply(result.split()[3], alpha))
    return result


def clone_board_layer(layer: BoardLayer, *, assign_new_id: bool = False) -> BoardLayer:
    """Deep-copy a board layer; optionally assign a new id."""
    layer_id = new_id() if assign_new_id else layer.id
    if isinstance(layer, ImageLayer):
        return ImageLayer(
            id=layer_id,
            # Sources are never edited in place, so copies can share the pixel data.
            source=layer.source,
            source_path=layer.source_path,
            x=layer.x,
            y=layer.y,
            width=layer.width,
            height=layer.height,
            opacity=layer.opacity,
            border_width=layer.border_width,
            border_color=layer.border_color,
            user_placed=layer.user_placed,
            collage_source=layer.collage_source,
            resize_percent=layer.resize_percent,
            applied_mask=layer.applied_mask,
            stroke_after_mask=layer.stroke_after_mask,
        )
    if isinstance(layer, MaskLayer):
        return replace(layer, id=layer_id)
    if isinstance(layer, LineLayer):
        return LineLayer(
            id=layer_id,
            x1=layer.x1,
            y1=layer.y1,
            x2=layer.x2,
            y2=layer.y2,
            color=layer.color,
            width=layer.width,
        )
    if isinstance(layer, RectLayer):
        return RectLayer(
            id=layer_id,
            x1=layer.x1,
            y1=layer.y1,
            x2=layer.x2,
            y2=layer.y2,
            outline=layer.outline,
            fill=layer.fill,
            width=layer.width,
        )
    if isinstance(layer, EllipseLayer):
        return EllipseLayer(
            id=layer_id,
            x1=layer.x1,
            y1=layer.y1,
            x2=layer.x2,
            y2=layer.y2,
            outline=layer.outline,
            fill=layer.fill,
            width=layer.width,
        )
    raise TypeError(f"Unknown layer type: {type(layer)!r}")


@dataclass
class BoardSnapshot:
    """Full poster state for undo and redo."""

    layers: list[BoardLayer]
    paint_layer: Image.Image
    selected_id: Optional[str]
    page_width: int
    page_height: int
    page_bg: str


@dataclass(frozen=True)
class AppliedMask:
    """What Apply mask replaced, kept so the mask can be edited again.

    The base_* fields describe the image before it was first masked. The applied_* fields
    describe the image as it looked right after the last Apply, so later changes can be
    detected. The mask_* fields are the mask's rectangle in page coordinates.
    """

    base_source: Image.Image
    base_x: float
    base_y: float
    base_w: float
    base_h: float
    applied_source: Image.Image
    applied_x: float
    applied_y: float
    applied_w: float
    applied_h: float
    mask_x: float
    mask_y: float
    mask_w: float
    mask_h: float
    shape: MaskShape
    corner_radius: int
    side: EdgeSide = "bottom"
    flipped: bool = False


@dataclass
class ImageLayer:
    """Raster image placed on the board."""

    kind: Literal["image"] = "image"
    id: str = field(default_factory=new_id)
    source: Image.Image = field(default_factory=lambda: Image.new("RGBA", (1, 1)))
    source_path: Optional[Path] = None
    x: float = 0.0
    y: float = 0.0
    width: float = 100.0
    height: float = 100.0
    opacity: float = 1.0
    border_width: int = 0
    border_color: str = "#000000"
    # Set once the user has moved or resized the image by dragging; Arrange images leaves it alone.
    user_placed: bool = False
    # Pixels from before the first Make collage, so later collage runs start over instead of stacking masks.
    collage_source: Optional[Image.Image] = None
    # Displayed size as a percent of the original; sets this image's share of space in Make collage and Arrange images.
    resize_percent: float = 100.0
    # Set by Apply mask; lets the mask be edited again while the image is unchanged.
    applied_mask: Optional[AppliedMask] = None
    # False: the border is a rectangle around the image, cut by any mask.
    # True: the border follows the masked shape's outline.
    stroke_after_mask: bool = False


@dataclass
class MaskLayer:
    """Shape that clips one image. Moves and resizes independently of the image it filters."""

    kind: Literal["mask"] = "mask"
    id: str = field(default_factory=new_id)
    target_id: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 100.0
    height: float = 100.0
    shape: MaskShape = "circle"
    corner_radius: int = 32
    # Edge shapes cut only one side: which side, and whether the cut is mirrored.
    side: EdgeSide = "bottom"
    flipped: bool = False
    # Set once the user has dragged or resized the mask; enables Apply mask.
    positioned: bool = False


@dataclass
class LineLayer:
    """Straight line stroke."""

    kind: Literal["line"] = "line"
    id: str = field(default_factory=new_id)
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    color: str = "#000000"
    width: int = 3


@dataclass
class RectLayer:
    """Axis-aligned rectangle."""

    kind: Literal["rect"] = "rect"
    id: str = field(default_factory=new_id)
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    outline: str = "#000000"
    fill: Optional[str] = None
    width: int = 3


@dataclass
class EllipseLayer:
    """Axis-aligned ellipse."""

    kind: Literal["ellipse"] = "ellipse"
    id: str = field(default_factory=new_id)
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    outline: str = "#000000"
    fill: Optional[str] = None
    width: int = 3


BoardLayer = Union[ImageLayer, MaskLayer, LineLayer, RectLayer, EllipseLayer]


def layer_display_name(layer: BoardLayer, stack_index: int) -> str:
    """Short label for layer pickers (1-based stack index)."""
    prefix = f"{stack_index + 1}."
    if isinstance(layer, ImageLayer):
        if layer.source_path is not None:
            return f"{prefix} {layer.source_path.name}"
        return f"{prefix} Image"
    if isinstance(layer, MaskLayer):
        return f"{prefix} Mask ({MASK_SHAPE_LABELS[layer.shape]})"
    return f"{prefix} {layer.kind.capitalize()}"


PAGE_PRESETS: dict[str, tuple[int, int]] = {
    "HD screen (1920×1080)": (1920, 1080),
    "Portrait phone (1080×1920)": (1080, 1920),
    "US Letter @ 150 DPI (1275×1650)": (1275, 1650),
    "US Letter @ 300 DPI (2550×3300)": (2550, 3300),
    "A4 @ 150 DPI (1240×1754)": (1240, 1754),
    "Poster 24×36 @ 150 DPI (3600×5400)": (3600, 5400),
}


def load_custom_page_presets() -> dict[str, tuple[int, int]]:
    """Return the page presets the user saved in Settings, skipping any that are invalid."""
    saved = load_settings().get("custom_page_presets")
    result: dict[str, tuple[int, int]] = {}
    if not isinstance(saved, dict):
        return result
    for name, size in saved.items():
        if not isinstance(name, str) or not name.strip() or name in PAGE_PRESETS:
            continue
        if not isinstance(size, list) or len(size) != 2:
            continue
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in size):
            continue
        if all(MIN_PAGE_SIZE <= v <= MAX_PAGE_SIZE for v in size):
            result[name] = (size[0], size[1])
    return result


def all_page_presets() -> dict[str, tuple[int, int]]:
    """Built-in page presets followed by the user's saved ones."""
    return {**PAGE_PRESETS, **load_custom_page_presets()}


class PosterCanvas(tk.Canvas):
    """Scrollable poster board with layers, tools, and zoom."""

    def __init__(
        self,
        master: tk.Misc,
        on_selection_change: Callable[[Optional[BoardLayer]], None],
        **kwargs: object,
    ) -> None:
        super().__init__(master, bg="#3d3d3d", highlightthickness=0, **kwargs)
        self._on_selection_change = on_selection_change
        # Default page: US Letter at 150 DPI, landscape.
        self.page_width = 1650
        self.page_height = 1275
        self.page_bg = "#ffffff"
        self.paint_layer = Image.new(
            "RGB",
            (self.page_width, self.page_height),
            self.page_bg,
        )
        self.layers: list[BoardLayer] = []
        self.tool: ToolName = "select"
        self.stroke_color = "#1a1a1a"
        self.fill_color = "#ffd966"
        self.stroke_width = 3
        self.fill_tolerance = 32
        self.zoom = 1.0
        # Selected layer ids; the last one is the active layer that the side panels edit.
        self.selected_ids: list[str] = []
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._board_photo: Optional[ImageTk.PhotoImage] = None
        self._drag_mode: Optional[str] = None
        self._drag_start_page: Optional[tuple[float, float]] = None
        self._drag_snapshots: dict[str, BoardLayer] = {}
        self._marquee_start: Optional[tuple[float, float]] = None
        self._marquee_end: Optional[tuple[float, float]] = None
        self._marquee_base: list[str] = []
        self._shape_start: Optional[tuple[float, float]] = None
        self._temp_shape_id: Optional[str] = None
        self._crop_target_id: Optional[str] = None
        self._crop_end_page: Optional[tuple[float, float]] = None
        self._undo_stack: list[BoardSnapshot] = []
        # Incremented on every undo checkpoint so panels can tell when history has moved on.
        self.history_version = 0
        self._redo_stack: list[BoardSnapshot] = []
        self._restoring_history = False
        self._clipboard_layers: list[BoardLayer] = []
        self._drag_had_undo = False

        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", self._on_wheel_linux)
        self.bind("<Button-5>", self._on_wheel_linux)
        # Tk reports Shift without saying which side, so track the right Shift key itself.
        self._right_shift_down = False
        # Corner resizes keep the image's aspect ratio unless the user turns this off in Settings.
        self.keep_aspect_on_resize = True
        self.bind_all("<KeyPress-Shift_R>", self._on_right_shift_press, add="+")
        self.bind_all("<KeyRelease-Shift_R>", self._on_right_shift_release, add="+")
        self.bind("<ButtonPress-3>", self._on_context_menu)
        self.bind("<Button-2>", self._on_context_menu)
        if self.tk.call("tk", "windowingsystem") == "aqua":
            # macOS: Ctrl+click is the conventional right-click.
            self.bind("<Control-Button-1>", self._on_context_menu)

    def set_page_size(self, width: int, height: int, bg: Optional[str] = None) -> None:
        """Resize the poster and reset the paint layer."""
        width = int(clamp(width, MIN_PAGE_SIZE, MAX_PAGE_SIZE))
        height = int(clamp(height, MIN_PAGE_SIZE, MAX_PAGE_SIZE))
        if width == self.page_width and height == self.page_height and bg is None:
            return
        self.push_undo_checkpoint()
        self.page_width = width
        self.page_height = height
        if bg is not None:
            self.page_bg = bg
        self.paint_layer = Image.new("RGB", (width, height), self.page_bg)
        self._fit_zoom()
        self.redraw()

    def capture_snapshot(self) -> BoardSnapshot:
        """Return a deep copy of the current board state."""
        return BoardSnapshot(
            layers=[clone_board_layer(layer) for layer in self.layers],
            paint_layer=self.paint_layer.copy(),
            selected_id=self.selected_id,
            page_width=self.page_width,
            page_height=self.page_height,
            page_bg=self.page_bg,
        )

    def _restore_snapshot(self, snapshot: BoardSnapshot) -> None:
        """Replace board state from a snapshot without recording undo."""
        self._restoring_history = True
        try:
            self.layers = [clone_board_layer(layer) for layer in snapshot.layers]
            self.paint_layer = snapshot.paint_layer.copy()
            self.selected_id = snapshot.selected_id
            if self.get_layer(self.selected_id) is None:
                self.selected_id = None
            self.page_width = snapshot.page_width
            self.page_height = snapshot.page_height
            self.page_bg = snapshot.page_bg
            layer = self.get_layer(self.selected_id) if self.selected_id else None
            self._on_selection_change(layer)
            self.redraw()
        finally:
            self._restoring_history = False

    def push_undo_checkpoint(self) -> None:
        """Save the current state on the undo stack and clear redo."""
        if self._restoring_history:
            return
        self._undo_stack.append(self.capture_snapshot())
        if len(self._undo_stack) > UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self.history_version += 1

    def clear_history(self) -> None:
        """Drop undo and redo stacks."""
        self._undo_stack.clear()
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        """Return True if undo is available."""
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        """Return True if redo is available."""
        return bool(self._redo_stack)

    def undo(self) -> bool:
        """Restore the previous board state."""
        if not self._undo_stack:
            return False
        self._redo_stack.append(self.capture_snapshot())
        snapshot = self._undo_stack.pop()
        self._restore_snapshot(snapshot)
        return True

    def redo(self) -> bool:
        """Reapply a undone board state."""
        if not self._redo_stack:
            return False
        self._undo_stack.append(self.capture_snapshot())
        snapshot = self._redo_stack.pop()
        self._restore_snapshot(snapshot)
        return True

    def copy_selected(self) -> bool:
        """Copy every selected layer to the internal clipboard.

        A mask is copied only together with the image it clips, so pasted masks stay attached.
        """
        selected = self.selected_layers()
        image_ids = {layer.id for layer in selected if isinstance(layer, ImageLayer)}
        copied = [
            clone_board_layer(layer)
            for layer in selected
            if not isinstance(layer, MaskLayer) or layer.target_id in image_ids
        ]
        if not copied:
            return False
        self._clipboard_layers = copied
        return True

    def cut_selected(self) -> bool:
        """Cut the selected layers to the internal clipboard."""
        if not self.copy_selected():
            return False
        self.push_undo_checkpoint()
        self.delete_selected(record_undo=False)
        return True

    def paste_clipboard(self) -> bool:
        """Paste the clipboard as new layers, offset from the originals and selected."""
        if not self._clipboard_layers:
            return False
        self.push_undo_checkpoint()
        id_map = {layer.id: new_id() for layer in self._clipboard_layers}
        pasted: list[BoardLayer] = []
        for source in self._clipboard_layers:
            layer = clone_board_layer(source)
            layer.id = id_map[source.id]
            if isinstance(layer, MaskLayer):
                layer.target_id = id_map[source.target_id]
            self._layer_move(layer, CLIPBOARD_PASTE_OFFSET, CLIPBOARD_PASTE_OFFSET)
            pasted.append(layer)
        self.layers.extend(pasted)
        self.set_selection([layer.id for layer in pasted])
        return True

    def set_tool(self, tool: ToolName) -> None:
        """Activate a canvas tool."""
        self.tool = tool
        if tool not in ("select", "crop"):
            self.select_layer(None)

    @property
    def selected_id(self) -> Optional[str]:
        """The active layer: the most recently selected one, which the side panels edit."""
        return self.selected_ids[-1] if self.selected_ids else None

    @selected_id.setter
    def selected_id(self, layer_id: Optional[str]) -> None:
        self.selected_ids = [layer_id] if layer_id else []

    def select_layer(self, layer_id: Optional[str], *, notify: bool = True) -> None:
        """Select a single layer by id, or clear the selection."""
        self.set_selection([layer_id] if layer_id else [], notify=notify)

    def set_selection(self, layer_ids: list[str], *, notify: bool = True) -> None:
        """Replace the selection. The last id becomes the active layer."""
        self.selected_ids = [layer_id for layer_id in dict.fromkeys(layer_ids) if self.get_layer(layer_id)]
        if notify:
            self._on_selection_change(self.selected_layer())
        else:
            self.redraw()

    def toggle_selection(self, layer_id: str) -> None:
        """Add a layer to the selection, or remove it if already selected (Shift/Ctrl-click)."""
        if layer_id in self.selected_ids:
            self.set_selection([i for i in self.selected_ids if i != layer_id])
        else:
            self.set_selection([*self.selected_ids, layer_id])

    def select_all(self) -> None:
        """Select every layer on the board."""
        self.set_selection([layer.id for layer in self.layers])

    def selected_layers(self) -> list[BoardLayer]:
        """Return every selected layer, in selection order."""
        return [layer for layer in (self.get_layer(i) for i in self.selected_ids) if layer is not None]

    def get_layer(self, layer_id: Optional[str]) -> Optional[BoardLayer]:
        """Return layer by id."""
        if layer_id is None:
            return None
        for layer in self.layers:
            if layer.id == layer_id:
                return layer
        return None

    def selected_layer(self) -> Optional[BoardLayer]:
        """Return the active layer."""
        return self.get_layer(self.selected_id)

    def displayed_stroke(self) -> tuple[str, int]:
        """Stroke colour and width of the selected shape, or the defaults when none is selected."""
        layer = self.selected_layer()
        if isinstance(layer, LineLayer):
            return layer.color, layer.width
        if isinstance(layer, (RectLayer, EllipseLayer)):
            return layer.outline, layer.width
        return self.stroke_color, self.stroke_width

    def apply_stroke_style(
        self,
        *,
        color: Optional[str] = None,
        width: Optional[int] = None,
        undo: bool = True,
    ) -> bool:
        """Update the default stroke for new shapes and restyle the selected shape.

        Returns True when the selected shape actually changed.
        """
        if color is not None:
            self.stroke_color = color
        if width is not None:
            self.stroke_width = width
        layer = self.selected_layer()
        if not isinstance(layer, (LineLayer, RectLayer, EllipseLayer)):
            return False
        cur_color, cur_width = self.displayed_stroke()
        new_color = color if color is not None else cur_color
        new_width = width if width is not None else cur_width
        if new_color == cur_color and new_width == cur_width:
            return False
        if undo:
            self.push_undo_checkpoint()
        if isinstance(layer, LineLayer):
            layer.color = new_color
        else:
            layer.outline = new_color
        layer.width = new_width
        self.redraw()
        return True

    def add_layer(self, layer: BoardLayer, *, record_undo: bool = True) -> None:
        """Append a layer and select it."""
        if record_undo and not self._restoring_history:
            self.push_undo_checkpoint()
        self.layers.append(layer)
        self.select_layer(layer.id)

    def delete_selected(self, *, record_undo: bool = True) -> None:
        """Remove the selected layer."""
        if not self.selected_ids:
            return
        if record_undo and not self._restoring_history:
            self.push_undo_checkpoint()
        doomed = set(self.selected_ids)
        # Deleting an image also removes the masks that clip it.
        self.layers = [
            layer
            for layer in self.layers
            if layer.id not in doomed
            and not (isinstance(layer, MaskLayer) and layer.target_id in doomed)
        ]
        self.select_layer(None)

    def move_layer_order(self, delta: int) -> None:
        """Move selected layer forward (delta=1) or backward (delta=-1)."""
        if self.selected_id is None:
            return
        idx = next(i for i, layer in enumerate(self.layers) if layer.id == self.selected_id)
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(self.layers):
            return
        self.push_undo_checkpoint()
        self.layers[idx], self.layers[new_idx] = self.layers[new_idx], self.layers[idx]
        self.redraw()

    def send_layer_to_front(self) -> None:
        """Move the selected layer to the top of the stack."""
        if self.selected_id is None:
            return
        self.push_undo_checkpoint()
        idx = next(i for i, layer in enumerate(self.layers) if layer.id == self.selected_id)
        layer = self.layers.pop(idx)
        self.layers.append(layer)
        self.redraw()

    def send_layer_to_back(self) -> None:
        """Move the selected layer to the bottom of the stack."""
        if self.selected_id is None:
            return
        self.push_undo_checkpoint()
        idx = next(i for i, layer in enumerate(self.layers) if layer.id == self.selected_id)
        layer = self.layers.pop(idx)
        self.layers.insert(0, layer)
        self.redraw()

    def _on_context_menu(self, event: tk.Event) -> None:
        """Show the stacking-order menu for the image under the cursor."""
        if self.tool != "select":
            return
        px, py = self.canvas_to_page(event.x, event.y)
        hit = self._hit_test(px, py)
        if not isinstance(hit, ImageLayer):
            return
        # Keep the rest of a multi-selection, but make the clicked layer the active one.
        self.set_selection([*[i for i in self.selected_ids if i != hit.id], hit.id])
        idx = next(i for i, layer in enumerate(self.layers) if layer.id == hit.id)
        last = len(self.layers) - 1

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Cut", command=self.cut_selected, accelerator="Ctrl+X")
        menu.add_command(label="Copy", command=self.copy_selected, accelerator="Ctrl+C")
        menu.add_command(
            label="Paste",
            command=self.paste_clipboard,
            accelerator="Ctrl+V",
            state="normal" if self._clipboard_layers else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Edit applied mask",
            command=lambda: self.edit_applied_mask(hit.id),
            state="normal" if self.can_edit_applied_mask(hit.id) else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Bring Forward",
            command=lambda: self.move_layer_order(1),
            state="normal" if idx < last else "disabled",
        )
        menu.add_command(
            label="Send Backward",
            command=lambda: self.move_layer_order(-1),
            state="normal" if idx > 0 else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Bring to Front",
            command=self.send_layer_to_front,
            state="normal" if idx < last else "disabled",
        )
        menu.add_command(
            label="Send to Back",
            command=self.send_layer_to_back,
            state="normal" if idx > 0 else "disabled",
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _layer_move(self, layer: BoardLayer, dx: float, dy: float) -> None:
        """Translate a layer by page-space delta."""
        if dx == 0 and dy == 0:
            return
        if isinstance(layer, ImageLayer):
            layer.x += dx
            layer.y += dy
        elif isinstance(layer, MaskLayer):
            layer.x += dx
            layer.y += dy
            layer.positioned = True
        elif isinstance(layer, (LineLayer, RectLayer, EllipseLayer)):
            layer.x1 += dx
            layer.y1 += dy
            layer.x2 += dx
            layer.y2 += dy

    @staticmethod
    def _alignment_delta(
        bbox: tuple[float, float, float, float],
        alignment: AlignMode,
        bounds: tuple[float, float, float, float],
    ) -> tuple[float, float]:
        """Return the (dx, dy) that moves bbox so it matches alignment relative to bounds."""
        x1, y1, x2, y2 = bbox
        bounds_x1, bounds_y1, bounds_x2, bounds_y2 = bounds
        width = x2 - x1
        height = y2 - y1
        dx = 0.0
        dy = 0.0
        if alignment == "left":
            dx = bounds_x1 - x1
        elif alignment == "right":
            dx = bounds_x2 - x2
        elif alignment == "center_h":
            dx = (bounds_x1 + bounds_x2) / 2 - width / 2 - x1
        elif alignment == "top":
            dy = bounds_y1 - y1
        elif alignment == "bottom":
            dy = bounds_y2 - y2
        elif alignment == "center_v":
            dy = (bounds_y1 + bounds_y2) / 2 - height / 2 - y1
        elif alignment == "center":
            dx = (bounds_x1 + bounds_x2) / 2 - width / 2 - x1
            dy = (bounds_y1 + bounds_y2) / 2 - height / 2 - y1
        return dx, dy

    def _selection_bbox(self, layers: list[BoardLayer]) -> tuple[float, float, float, float]:
        """Union bounding box of several layers."""
        boxes = [self._layer_bbox(layer) for layer in layers]
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    def _align_selection(self, alignment: AlignMode, bounds: tuple[float, float, float, float]) -> bool:
        """Move the whole selection as one unit so its bounding box aligns to bounds."""
        layers = self.selected_layers()
        if not layers:
            return False
        self.push_undo_checkpoint()
        dx, dy = self._alignment_delta(self._selection_bbox(layers), alignment, bounds)
        for layer in layers:
            self._layer_move(layer, dx, dy)
        self._on_selection_change(self.selected_layer())
        self.redraw()
        return True

    def align_selected(self, alignment: AlignMode) -> None:
        """Align the selection (as one unit) to a page edge or center."""
        self._align_selection(alignment, (0.0, 0.0, self.page_width, self.page_height))

    def align_selected_to_layer(self, alignment: AlignMode, reference_id: str) -> None:
        """Align the selection (as one unit) to the reference layer's bounding box."""
        reference = self.get_layer(reference_id)
        if reference is None or reference_id in self.selected_ids:
            return
        self._align_selection(alignment, self._layer_bbox(reference))

    def arrange_images(
        self,
        margin: float = ARRANGE_DEFAULT_MARGIN,
        gap: float = ARRANGE_DEFAULT_GAP,
    ) -> bool:
        """Position and size the images not moved by hand so they fit on the page without overlap."""
        # Images the user has dragged keep their place and size.
        images = [layer for layer in self.layers if isinstance(layer, ImageLayer) and not layer.user_placed]
        if not images:
            return False
        aspects = [layer.source.width / max(1, layer.source.height) for layer in images]
        weights = [layer.resize_percent for layer in images]
        rects = compute_arrangement(aspects, self.page_width, self.page_height, margin, gap, weights)
        if not rects:
            return False

        self.push_undo_checkpoint()
        for layer, aspect, (x, y, w, h) in zip(images, aspects, rects):
            # The border is drawn outside the bbox, so fit the image inside the cell
            # minus the border on each side, keeping the source aspect ratio.
            bw = max(0, layer.border_width)
            inner_w = max(MIN_LAYER_SIZE, w - 2 * bw)
            inner_h = max(MIN_LAYER_SIZE, h - 2 * bw)
            img_w = min(inner_w, inner_h * aspect)
            img_h = img_w / aspect
            layer.width = img_w
            layer.height = img_h
            layer.x = x + bw + (inner_w - img_w) / 2
            layer.y = y + bw + (inner_h - img_h) / 2

        self._on_selection_change(self.selected_layer())
        self.redraw()
        return True

    def make_collage(
        self,
        shape: MaskShape,
        *,
        max_overlap: float = 0.10,
        stroke_width: int = 0,
        stroke_color: str = "#000000",
        corner_fraction: float = COLLAGE_DEFAULT_CORNER_PCT / 100,
    ) -> Optional[tuple[float, float]]:
        """Fill the page with every image, each cropped to its cell and masked with shape.

        max_overlap is a fraction of covered area. A stroke, when set, is drawn just inside
        each cell and follows the masked shape.
        Returns (coverage, overlap) as fractions, or None when there was nothing to lay out.
        """
        images = [layer for layer in self.layers if isinstance(layer, ImageLayer)]
        if not images:
            return None
        aspects = [_collage_base(layer).width / max(1, _collage_base(layer).height) for layer in images]
        rects, coverage, overlap = compute_collage(
            aspects,
            self.page_width,
            self.page_height,
            shape,
            max_overlap=max_overlap,
            corner_fraction=corner_fraction,
            weights=[layer.resize_percent for layer in images],
        )
        if not rects:
            return None

        bw = max(0, int(stroke_width))
        self.push_undo_checkpoint()
        # Masks left over from earlier edits no longer match the new layout; the collage replaces them.
        laid_out = {layer.id for layer in images}
        self.layers = [
            layer for layer in self.layers
            if not (isinstance(layer, MaskLayer) and layer.target_id in laid_out)
        ]
        self.set_selection([i for i in self.selected_ids if self.get_layer(i) is not None])
        for layer, (x, y, w, h) in zip(images, rects):
            # The stroke is drawn outside the image's box, so inset the box to keep it in the cell.
            x, y = x + bw, y + bw
            w = max(MIN_LAYER_SIZE, w - 2 * bw)
            h = max(MIN_LAYER_SIZE, h - 2 * bw)
            base = _collage_base(layer)
            prior = layer.applied_mask
            if layer.collage_source is not None and prior is not None:
                # A re-run keeps the geometry from before the first collage, so Edit applied mask restores it.
                original = (prior.base_x, prior.base_y, prior.base_w, prior.base_h)
            else:
                original = (layer.x, layer.y, layer.width, layer.height)
            layer.collage_source = base
            cropped = center_crop_to_aspect(base.convert("RGBA"), w / h)
            # Bake the mask into the pixels, as the existing mask apply does, so the
            # shape survives save/export without a separate mask layer.
            alpha = create_shape_mask(
                cropped.width,
                cropped.height,
                shape,
                corner_radius=int(corner_fraction * min(cropped.width, cropped.height)),
            )
            cropped.putalpha(ImageChops.multiply(cropped.split()[3], alpha))
            layer.source = cropped
            layer.x, layer.y, layer.width, layer.height = x, y, w, h
            layer.border_width = bw
            layer.border_color = stroke_color
            layer.stroke_after_mask = bw > 0
            # Record the baked mask like Apply mask does, so Edit applied mask can bring it back.
            layer.applied_mask = AppliedMask(
                base_source=base,
                base_x=original[0],
                base_y=original[1],
                base_w=original[2],
                base_h=original[3],
                applied_source=cropped,
                applied_x=x,
                applied_y=y,
                applied_w=w,
                applied_h=h,
                mask_x=x,
                mask_y=y,
                mask_w=w,
                mask_h=h,
                shape=shape,
                corner_radius=int(corner_fraction * min(w, h)),
            )

        self._on_selection_change(self.selected_layer())
        self.redraw()
        return coverage, overlap

    def add_mask_for_image(self, image: ImageLayer, shape: MaskShape = "circle") -> Optional[MaskLayer]:
        """Create a mask covering the image (or select its existing mask) and select it.

        Returns None when the image already has a mask applied; that mask is edited instead.
        """
        existing = self.masks_for(image.id)
        if existing:
            self.select_layer(existing[0].id)
            return existing[0]
        prior = image.applied_mask
        if prior is not None and self._applied_state_intact(image, prior):
            return None
        self.push_undo_checkpoint()
        mask = MaskLayer(
            target_id=image.id,
            x=image.x,
            y=image.y,
            width=image.width,
            height=image.height,
            shape=shape,
        )
        self.layers.append(mask)
        self.select_layer(mask.id)
        return mask

    @staticmethod
    def _applied_state_intact(image: ImageLayer, prior: AppliedMask) -> bool:
        """True while the image still looks as it did right after Apply mask (moves are allowed)."""
        return (
            image.source is prior.applied_source
            and image.width == prior.applied_w
            and image.height == prior.applied_h
        )

    def can_edit_applied_mask(self, image_id: str) -> bool:
        image = self.get_layer(image_id)
        if not isinstance(image, ImageLayer) or image.applied_mask is None:
            return False
        return self._applied_state_intact(image, image.applied_mask) and not self.masks_for(image_id)

    def edit_applied_mask(self, image_id: str) -> bool:
        """Put the image back to its pre-mask pixels and bring its mask back as a live, editable layer."""
        if not self.can_edit_applied_mask(image_id):
            return False
        image = self.get_layer(image_id)
        assert isinstance(image, ImageLayer) and image.applied_mask is not None
        prior = image.applied_mask
        self.push_undo_checkpoint()
        # Keep any move made since Apply, so the restored image and mask stay where they were.
        dx = image.x - prior.applied_x
        dy = image.y - prior.applied_y
        image.source = prior.base_source
        image.x = prior.base_x + dx
        image.y = prior.base_y + dy
        image.width = prior.base_w
        image.height = prior.base_h
        image.applied_mask = None
        mask = MaskLayer(
            target_id=image.id,
            x=prior.mask_x + dx,
            y=prior.mask_y + dy,
            width=prior.mask_w,
            height=prior.mask_h,
            shape=prior.shape,
            corner_radius=prior.corner_radius,
            side=prior.side,
            flipped=prior.flipped,
            positioned=True,
        )
        self.layers.append(mask)
        self.select_layer(mask.id)
        return True

    def apply_mask(self, mask_id: str) -> bool:
        """Bake a mask into its image: crop the image to the overlap, clip its pixels, drop the mask."""
        mask = self.get_layer(mask_id)
        if not isinstance(mask, MaskLayer):
            return False
        image = self.get_layer(mask.target_id)
        if not isinstance(image, ImageLayer):
            return False

        ix1 = max(image.x, mask.x)
        iy1 = max(image.y, mask.y)
        ix2 = min(image.x + image.width, mask.x + mask.width)
        iy2 = min(image.y + image.height, mask.y + mask.height)
        if ix2 - ix1 < MIN_LAYER_SIZE or iy2 - iy1 < MIN_LAYER_SIZE:
            return False

        sw, sh = image.source.width, image.source.height
        if sw < 1 or sh < 1 or image.width < 1 or image.height < 1:
            return False
        scale_x = sw / image.width
        scale_y = sh / image.height
        sx1 = clamp(round((ix1 - image.x) * scale_x), 0, sw - 1)
        sy1 = clamp(round((iy1 - image.y) * scale_y), 0, sh - 1)
        sx2 = clamp(round((ix2 - image.x) * scale_x), sx1 + 1, sw)
        sy2 = clamp(round((iy2 - image.y) * scale_y), sy1 + 1, sh)

        cropped = image.source.crop((sx1, sy1, sx2, sy2)).convert("RGBA")
        # Page position of the crop's top-left corner, so the mask lines up with the pixels.
        origin_x = image.x + sx1 / scale_x
        origin_y = image.y + sy1 / scale_y
        alpha = mask_alpha(cropped.width, cropped.height, mask, origin_x, origin_y, scale_x, scale_y)
        cropped.putalpha(ImageChops.multiply(cropped.split()[3], alpha))

        self.push_undo_checkpoint()
        base = (image.source, image.x, image.y, image.width, image.height)
        image.source = cropped
        image.x = origin_x
        image.y = origin_y
        image.width = cropped.width / scale_x
        image.height = cropped.height / scale_y
        image.applied_mask = AppliedMask(
            base_source=base[0],
            base_x=base[1],
            base_y=base[2],
            base_w=base[3],
            base_h=base[4],
            applied_source=image.source,
            applied_x=image.x,
            applied_y=image.y,
            applied_w=image.width,
            applied_h=image.height,
            mask_x=mask.x,
            mask_y=mask.y,
            mask_w=mask.width,
            mask_h=mask.height,
            shape=mask.shape,
            corner_radius=mask.corner_radius,
            side=mask.side,
            flipped=mask.flipped,
        )
        self.layers = [layer for layer in self.layers if layer.id != mask.id]
        self.select_layer(image.id, notify=False)
        self._on_selection_change(image)
        self.redraw()
        return True

    def _apply_image_crop(
        self,
        layer: ImageLayer,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> None:
        """Crop an image layer using a page-coordinate drag rectangle."""
        rx1, rx2 = min(x1, x2), max(x1, x2)
        ry1, ry2 = min(y1, y2), max(y1, y2)
        ix1 = max(rx1, layer.x)
        iy1 = max(ry1, layer.y)
        ix2 = min(rx2, layer.x + layer.width)
        iy2 = min(ry2, layer.y + layer.height)
        if ix2 - ix1 < MIN_LAYER_SIZE or iy2 - iy1 < MIN_LAYER_SIZE:
            return

        sw, sh = layer.source.width, layer.source.height
        if sw < 1 or sh < 1 or layer.width < 1 or layer.height < 1:
            return

        sx1 = int((ix1 - layer.x) / layer.width * sw)
        sy1 = int((iy1 - layer.y) / layer.height * sh)
        sx2 = int((ix2 - layer.x) / layer.width * sw)
        sy2 = int((iy2 - layer.y) / layer.height * sh)
        sx1 = clamp(sx1, 0, sw - 1)
        sy1 = clamp(sy1, 0, sh - 1)
        sx2 = clamp(sx2, sx1 + 1, sw)
        sy2 = clamp(sy2, sy1 + 1, sh)

        layer.source = layer.source.crop((sx1, sy1, sx2, sy2))
        layer.x = ix1
        layer.y = iy1
        layer.width = ix2 - ix1
        layer.height = iy2 - iy1

    def add_image_from_file(
        self,
        path: Path,
        *,
        placement_index: int = 0,
        report_errors: bool = True,
        record_undo: bool = True,
    ) -> bool:
        """Load an image file and place it on the board. Return True on success."""
        if not is_image_path(path):
            return False
        try:
            with Image.open(path) as img:
                img.load()
                rgba = img.convert("RGBA")
        except OSError as exc:
            if report_errors:
                messagebox.showerror("Import failed", f"{path.name}: {exc}")
            return False

        max_w = self.page_width * 0.45
        max_h = self.page_height * 0.45
        scale = min(max_w / rgba.width, max_h / rgba.height, 1.0)
        w = max(MIN_LAYER_SIZE, rgba.width * scale)
        h = max(MIN_LAYER_SIZE, rgba.height * scale)
        stagger = 24 * placement_index
        layer = ImageLayer(
            source=rgba,
            source_path=path,
            x=(self.page_width - w) / 2 + stagger,
            y=(self.page_height - h) / 2 + stagger,
            width=w,
            height=h,
        )
        self.add_layer(layer, record_undo=record_undo)
        return True

    def add_images_from_files(self, paths: list[Path]) -> None:
        """Import several image files; stagger placement so they do not fully overlap."""
        failed: list[str] = []
        placed = 0
        if paths:
            self.push_undo_checkpoint()
        for path in paths:
            if self.add_image_from_file(
                path,
                placement_index=placed,
                report_errors=False,
                record_undo=False,
            ):
                placed += 1
            else:
                failed.append(path.name)
        if failed:
            messagebox.showwarning(
                "Import",
                "Could not import:\n" + "\n".join(failed),
            )

    def masks_for(self, image_id: str) -> list[MaskLayer]:
        """Return the mask layers that clip the given image."""
        return [layer for layer in self.layers if isinstance(layer, MaskLayer) and layer.target_id == image_id]

    def render_to_image(self) -> Image.Image:
        """Composite the board at full page resolution. Mask layers clip their image; they draw nothing."""
        base = self.paint_layer.convert("RGBA")

        for layer in self.layers:
            if isinstance(layer, ImageLayer):
                composited, ox, oy = compose_image_layer(layer, self.masks_for(layer.id))
                base.paste(composited, (ox, oy), composited)
            elif isinstance(layer, MaskLayer):
                continue
            elif isinstance(layer, LineLayer):
                draw = ImageDraw.Draw(base)
                draw.line(
                    (layer.x1, layer.y1, layer.x2, layer.y2),
                    fill=layer.color,
                    width=max(1, layer.width),
                )
            elif isinstance(layer, RectLayer):
                draw = ImageDraw.Draw(base)
                draw.rectangle(
                    (layer.x1, layer.y1, layer.x2, layer.y2),
                    outline=layer.outline,
                    fill=layer.fill,
                    width=max(1, layer.width),
                )
            elif isinstance(layer, EllipseLayer):
                draw = ImageDraw.Draw(base)
                draw.ellipse(
                    (layer.x1, layer.y1, layer.x2, layer.y2),
                    outline=layer.outline,
                    fill=layer.fill,
                    width=max(1, layer.width),
                )
        return base

    def _fit_zoom(self) -> None:
        margin = 48
        cw = max(self.winfo_width() - margin, 1)
        ch = max(self.winfo_height() - margin, 1)
        self.zoom = min(cw / self.page_width, ch / self.page_height, 1.0)

    def _board_offset(self) -> tuple[float, float]:
        disp_w = self.page_width * self.zoom
        disp_h = self.page_height * self.zoom
        ox = (self.winfo_width() - disp_w) / 2
        oy = (self.winfo_height() - disp_h) / 2
        return ox, oy

    def page_to_canvas(self, px: float, py: float) -> tuple[float, float]:
        """Map page coordinates to canvas coordinates."""
        ox, oy = self._board_offset()
        return ox + px * self.zoom, oy + py * self.zoom

    def canvas_to_page(self, cx: float, cy: float) -> tuple[float, float]:
        """Map canvas coordinates to page coordinates."""
        ox, oy = self._board_offset()
        return (cx - ox) / self.zoom, (cy - oy) / self.zoom

    def _layer_bbox(self, layer: BoardLayer) -> tuple[float, float, float, float]:
        if isinstance(layer, (ImageLayer, MaskLayer)):
            return layer.x, layer.y, layer.x + layer.width, layer.y + layer.height
        if isinstance(layer, (LineLayer, RectLayer, EllipseLayer)):
            return (
                min(layer.x1, layer.x2),
                min(layer.y1, layer.y2),
                max(layer.x1, layer.x2),
                max(layer.y1, layer.y2),
            )
        return 0.0, 0.0, 0.0, 0.0

    def _hit_test(self, px: float, py: float) -> Optional[BoardLayer]:
        for layer in reversed(self.layers):
            x1, y1, x2, y2 = self._layer_bbox(layer)
            if x1 <= px <= x2 and y1 <= py <= y2:
                if isinstance(layer, LineLayer):
                    if self._distance_to_segment(px, py, layer.x1, layer.y1, layer.x2, layer.y2) <= 8:
                        return layer
                else:
                    return layer
        return None

    @staticmethod
    def _distance_to_segment(
        px: float,
        py: float,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> float:
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0 and dy == 0:
            return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
        t = clamp(((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy), 0.0, 1.0)
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy
        return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5

    def _hit_handle(self, px: float, py: float, layer: Union[ImageLayer, MaskLayer]) -> Optional[str]:
        """Return the corner handle under the pointer, measured in screen pixels.

        Page-unit tolerances grow as the view zooms out, which made the middle of a small
        mask count as a corner handle; comparing on screen keeps the grab area fixed.
        """
        x1, y1, x2, y2 = self._layer_bbox(layer)
        points = {
            "nw": (x1, y1),
            "ne": (x2, y1),
            "sw": (x1, y2),
            "se": (x2, y2),
        }
        qx, qy = self.page_to_canvas(px, py)
        for name, (hx, hy) in points.items():
            cx, cy = self.page_to_canvas(hx, hy)
            if abs(qx - cx) <= HANDLE_RADIUS and abs(qy - cy) <= HANDLE_RADIUS:
                return name
        return None

    def _on_configure(self, _event: tk.Event) -> None:
        self._fit_zoom()
        self.redraw()

    def _on_wheel(self, event: tk.Event) -> None:
        if event.delta > 0:
            self.zoom = min(self.zoom * 1.1, 4.0)
        else:
            self.zoom = max(self.zoom / 1.1, 0.05)
        self.redraw()

    def _on_wheel_linux(self, event: tk.Event) -> None:
        if event.num == 4:
            self.zoom = min(self.zoom * 1.1, 4.0)
        else:
            self.zoom = max(self.zoom / 1.1, 0.05)
        self.redraw()

    def _on_press(self, event: tk.Event) -> None:
        px, py = self.canvas_to_page(event.x, event.y)
        if not (0 <= px <= self.page_width and 0 <= py <= self.page_height):
            if self.tool == "select":
                self.select_layer(None)
            return

        if self.tool == "fill":
            self.push_undo_checkpoint()
            self._flood_fill(int(px), int(py))
            self.redraw()
            return

        if self.tool == "select":
            additive = bool(event.state & (SHIFT_MASK | CTRL_MASK))
            layer = self.selected_layer()
            if len(self.selected_ids) == 1 and isinstance(layer, (ImageLayer, MaskLayer)):
                handle = self._hit_handle(px, py, layer)
                if handle:
                    self._begin_drag_undo()
                    self._drag_mode = handle
                    self._drag_start_page = (px, py)
                    self._drag_snapshots = {layer.id: replace(layer)}
                    return
            hit = self._hit_test(px, py)
            if hit is None:
                # Empty space starts a marquee; Shift/Ctrl keeps the current selection.
                self._marquee_base = list(self.selected_ids) if additive else []
                self._marquee_start = (px, py)
                self._marquee_end = (px, py)
                if not additive:
                    self.select_layer(None)
                return
            if additive:
                self.toggle_selection(hit.id)
                if hit.id not in self.selected_ids:
                    return
            elif hit.id not in self.selected_ids:
                self.select_layer(hit.id)
            # Move the whole selection from the clicked point.
            self._begin_drag_undo()
            self._drag_mode = "move"
            self._drag_start_page = (px, py)
            self._drag_snapshots = {layer.id: replace(layer) for layer in self.selected_layers()}
            return

        if self.tool == "crop":
            hit = self._hit_test(px, py)
            if isinstance(hit, ImageLayer):
                self.select_layer(hit.id)
                self._crop_target_id = hit.id
                self._shape_start = (px, py)
                self._crop_end_page = (px, py)
            return

        self.push_undo_checkpoint()
        self._shape_start = (px, py)
        if self.tool == "line":
            temp = LineLayer(
                x1=px,
                y1=py,
                x2=px,
                y2=py,
                color=self.stroke_color,
                width=self.stroke_width,
            )
            self._temp_shape_id = temp.id
            self.layers.append(temp)
            self.select_layer(temp.id, notify=False)
        elif self.tool == "rect":
            temp = RectLayer(
                x1=px,
                y1=py,
                x2=px,
                y2=py,
                outline=self.stroke_color,
                fill=self.fill_color if self.fill_color else None,
                width=self.stroke_width,
            )
            self._temp_shape_id = temp.id
            self.layers.append(temp)
            self.select_layer(temp.id, notify=False)
        elif self.tool == "ellipse":
            temp = EllipseLayer(
                x1=px,
                y1=py,
                x2=px,
                y2=py,
                outline=self.stroke_color,
                fill=self.fill_color if self.fill_color else None,
                width=self.stroke_width,
            )
            self._temp_shape_id = temp.id
            self.layers.append(temp)
            self.select_layer(temp.id, notify=False)

    def _begin_drag_undo(self) -> None:
        """Record one undo step at the start of a drag resize or move."""
        if self._drag_had_undo:
            return
        self.push_undo_checkpoint()
        self._drag_had_undo = True

    def _on_right_shift_press(self, _event: tk.Event) -> None:
        self._right_shift_down = True

    def _on_right_shift_release(self, _event: tk.Event) -> None:
        self._right_shift_down = False

    def _on_motion(self, event: tk.Event) -> None:
        px, py = self.canvas_to_page(event.x, event.y)
        px = clamp(px, 0, self.page_width)
        py = clamp(py, 0, self.page_height)

        if self._marquee_start is not None:
            self._marquee_end = (px, py)
            self.redraw()
            return

        if self._drag_mode and self._drag_start_page and self._drag_snapshots:
            sx, sy = self._drag_start_page
            dx, dy = px - sx, py - sy
            if self._drag_mode == "move":
                for layer_id, snap in self._drag_snapshots.items():
                    layer = self.get_layer(layer_id)
                    if layer is not None:
                        self._place_from_snapshot(layer, snap, dx, dy)
                self.redraw()
                return
            # Resize: only ever a single image or mask (see _on_press).
            layer_id, snap = next(iter(self._drag_snapshots.items()))
            layer = self.get_layer(layer_id)
            if not isinstance(layer, (ImageLayer, MaskLayer)) or not isinstance(snap, (ImageLayer, MaskLayer)):
                return
            x1, y1, x2, y2 = snap.x, snap.y, snap.x + snap.width, snap.y + snap.height
            if self._drag_mode in ("se", "ne"):
                x2 = x2 + dx
            if self._drag_mode in ("sw", "nw"):
                x1 = x1 + dx
            if self._drag_mode in ("se", "sw"):
                y2 = y2 + dy
            if self._drag_mode in ("ne", "nw"):
                y1 = y1 + dy
            # Right Shift forces the lock even when the setting is off.
            if isinstance(layer, ImageLayer) and (self.keep_aspect_on_resize or self._right_shift_down) and snap.height > 0:
                x1, y1, x2, y2 = aspect_locked_corner_rect(
                    self._drag_mode,
                    x1,
                    y1,
                    x2,
                    y2,
                    snap.width / snap.height,
                    MIN_LAYER_SIZE,
                )
            if x2 - x1 < MIN_LAYER_SIZE:
                x2 = x1 + MIN_LAYER_SIZE
            if y2 - y1 < MIN_LAYER_SIZE:
                y2 = y1 + MIN_LAYER_SIZE
            layer.x = x1
            layer.y = y1
            layer.width = x2 - x1
            layer.height = y2 - y1
            if isinstance(layer, ImageLayer):
                record_resize_percent(layer)
            if isinstance(layer, ImageLayer) and (abs(dx) >= 1 or abs(dy) >= 1):
                layer.user_placed = True
            if isinstance(layer, MaskLayer):
                layer.positioned = True
            self.redraw()
            return

        if self.tool == "crop" and self._crop_target_id and self._shape_start:
            self._crop_end_page = (px, py)
            self.redraw()
            return

        if self._shape_start and self._temp_shape_id:
            layer = self.get_layer(self._temp_shape_id)
            if layer is None:
                return
            sx, sy = self._shape_start
            if isinstance(layer, LineLayer):
                layer.x2, layer.y2 = px, py
            elif isinstance(layer, (RectLayer, EllipseLayer)):
                layer.x2, layer.y2 = px, py
            self.redraw()

    @staticmethod
    def _place_from_snapshot(layer: BoardLayer, snap: BoardLayer, dx: float, dy: float) -> None:
        """Position a layer at its drag-start snapshot plus the drag offset."""
        if isinstance(layer, (ImageLayer, MaskLayer)) and isinstance(snap, (ImageLayer, MaskLayer)):
            layer.x = snap.x + dx
            layer.y = snap.y + dy
            if isinstance(layer, MaskLayer):
                layer.positioned = True
        elif isinstance(layer, (LineLayer, RectLayer, EllipseLayer)) and isinstance(
            snap,
            (LineLayer, RectLayer, EllipseLayer),
        ):
            layer.x1 = snap.x1 + dx
            layer.y1 = snap.y1 + dy
            layer.x2 = snap.x2 + dx
            layer.y2 = snap.y2 + dy

    def _finish_marquee(self) -> None:
        """Select every layer the marquee touches, adding to the base selection if Shift/Ctrl was held."""
        start = self._marquee_start
        end = self._marquee_end or start
        base = self._marquee_base
        self._marquee_start = None
        self._marquee_end = None
        self._marquee_base = []
        if start is None or end is None:
            return
        x1, x2 = sorted((start[0], end[0]))
        y1, y2 = sorted((start[1], end[1]))
        if x2 - x1 < 3 and y2 - y1 < 3:
            # A plain click on empty space: keep only what was already selected (if additive).
            self.set_selection(base)
            return
        hits = [
            layer.id
            for layer in self.layers
            if self._boxes_overlap(self._layer_bbox(layer), (x1, y1, x2, y2))
        ]
        self.set_selection([*base, *hits])

    @staticmethod
    def _boxes_overlap(
        a: tuple[float, float, float, float],
        b: tuple[float, float, float, float],
    ) -> bool:
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

    def _on_release(self, _event: tk.Event) -> None:
        if self._marquee_start is not None:
            self._finish_marquee()
        if (
            self.tool == "crop"
            and self._crop_target_id
            and self._shape_start
            and self._crop_end_page
        ):
            layer = self.get_layer(self._crop_target_id)
            if isinstance(layer, ImageLayer):
                sx, sy = self._shape_start
                ex, ey = self._crop_end_page
                self.push_undo_checkpoint()
                self._apply_image_crop(layer, sx, sy, ex, ey)
        self._crop_target_id = None
        self._crop_end_page = None

        self._drag_mode = None
        self._drag_start_page = None
        self._drag_snapshots = {}
        self._drag_had_undo = False
        self._shape_start = None
        temp_id = self._temp_shape_id
        self._temp_shape_id = None
        layer = self.get_layer(temp_id) if temp_id else self.selected_layer()
        if layer is not None:
            self._on_selection_change(layer)

    def _flood_fill(self, px: int, py: int) -> None:
        if not (0 <= px < self.page_width and 0 <= py < self.page_height):
            return
        target = self.paint_layer.getpixel((px, py))
        fill_rgb = self._hex_to_rgb(self.fill_color)
        if target == fill_rgb:
            return
        ImageDraw.floodfill(
            self.paint_layer,
            (px, py),
            fill_rgb,
            thresh=self.fill_tolerance,
        )

    @staticmethod
    def _hex_to_rgb(color: str) -> tuple[int, int, int]:
        color = color.lstrip("#")
        if len(color) == 6:
            return (
                int(color[0:2], 16),
                int(color[2:4], 16),
                int(color[4:6], 16),
            )
        return 0, 0, 0

    def redraw(self) -> None:
        """Refresh the canvas from layer state."""
        self.delete("all")
        ox, oy = self._board_offset()
        disp_w = self.page_width * self.zoom
        disp_h = self.page_height * self.zoom

        preview = self.render_to_image()
        preview = preview.resize(
            (max(1, int(disp_w)), max(1, int(disp_h))),
            Image.Resampling.LANCZOS,
        )
        self._board_photo = ImageTk.PhotoImage(preview)
        self.create_image(ox, oy, anchor="nw", image=self._board_photo, tags="board")
        self.create_rectangle(
            ox,
            oy,
            ox + disp_w,
            oy + disp_h,
            outline="#666666",
            width=1,
            tags="board",
        )

        # Masks are editor-only outlines; they are never drawn into the exported image.
        for mask in self.layers:
            if isinstance(mask, MaskLayer) and mask.id not in self.selected_ids:
                mx1, my1 = self.page_to_canvas(mask.x, mask.y)
                mx2, my2 = self.page_to_canvas(mask.x + mask.width, mask.y + mask.height)
                self.create_rectangle(mx1, my1, mx2, my2, outline=MASK_OUTLINE_COLOR, width=1, dash=(2, 4))

        for selected in self.selected_layers():
            bx1, by1, bx2, by2 = self._layer_bbox(selected)
            sx1, sy1 = self.page_to_canvas(bx1, by1)
            sx2, sy2 = self.page_to_canvas(bx2, by2)
            highlight = MASK_OUTLINE_COLOR if isinstance(selected, MaskLayer) else "#4a9eff"
            self.create_rectangle(sx1, sy1, sx2, sy2, outline=highlight, width=2, dash=(4, 2))

        layer = self.selected_layer()
        if len(self.selected_ids) == 1 and isinstance(layer, (ImageLayer, MaskLayer)):
            color = MASK_OUTLINE_COLOR if isinstance(layer, MaskLayer) else "#4a9eff"
            x1, y1 = self.page_to_canvas(layer.x, layer.y)
            x2, y2 = self.page_to_canvas(layer.x + layer.width, layer.y + layer.height)
            for hx, hy in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
                self.create_oval(
                    hx - HANDLE_RADIUS,
                    hy - HANDLE_RADIUS,
                    hx + HANDLE_RADIUS,
                    hy + HANDLE_RADIUS,
                    fill="#ffffff",
                    outline=color,
                    width=2,
                )

        if self._marquee_start is not None and self._marquee_end is not None:
            mx1, my1 = self.page_to_canvas(*self._marquee_start)
            mx2, my2 = self.page_to_canvas(*self._marquee_end)
            self.create_rectangle(mx1, my1, mx2, my2, outline="#4a9eff", width=1, dash=(3, 3))

        if self._crop_target_id and self._shape_start and self._crop_end_page:
            sx, sy = self._shape_start
            ex, ey = self._crop_end_page
            cx1, cy1 = self.page_to_canvas(min(sx, ex), min(sy, ey))
            cx2, cy2 = self.page_to_canvas(max(sx, ex), max(sy, ey))
            self.create_rectangle(
                cx1,
                cy1,
                cx2,
                cy2,
                outline="#e67e22",
                width=2,
                dash=(6, 3),
            )


# Print tiling: paper sizes in inches, (width, height) portrait.
PRINT_PAPERS: dict[str, tuple[float, float]] = {
    "US Letter (8.5 × 11 in)": (8.5, 11.0),
    "A4 (8.27 × 11.69 in)": (8.27, 11.69),
}
PRINT_PPI_MIN = 50
PRINT_PPI_MAX = 600
PRINT_MAX_SHEETS = 60  # every sheet is held in memory while the PDF is written
PRINT_CONFIRM_SHEETS = 20
PRINT_DEFAULTS: dict[str, object] = {
    "paper": "US Letter (8.5 × 11 in)",
    "landscape": False,
    "ppi": 150,
    "overlap": 0.5,
    "margin": 0.25,
}


def plan_print_tiles(
    board_w_px: int,
    board_h_px: int,
    ppi: int,
    sheet_w_in: float,
    sheet_h_in: float,
    margin_in: float,
    overlap_in: float,
) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """Plan the sheets that cover the board.

    Each sheet prints a content area inside its margins. Neighbouring sheets share
    overlap_in inches, so they can be glued along that band. Returns (rows, cols, tiles)
    where each tile is (row, col, x_px, y_px): the content area's top-left corner in board pixels.
    """
    content_w = sheet_w_in - 2 * margin_in
    content_h = sheet_h_in - 2 * margin_in
    step_x = content_w - overlap_in
    step_y = content_h - overlap_in
    if content_w <= 0 or content_h <= 0 or step_x <= 0 or step_y <= 0:
        raise ValueError("The margin and glue overlap leave no printable area on the sheet.")
    board_w_in = board_w_px / ppi
    board_h_in = board_h_px / ppi
    cols = max(1, math.ceil((board_w_in - overlap_in) / step_x))
    rows = max(1, math.ceil((board_h_in - overlap_in) / step_y))
    tiles = [
        (r, c, round(c * step_x * ppi), round(r * step_y * ppi))
        for r in range(rows)
        for c in range(cols)
    ]
    return rows, cols, tiles


def _dashed_line(draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int, fill: tuple[int, int, int]) -> None:
    """Draw a dashed horizontal or vertical line."""
    dash, gap = 14, 8
    if x0 == x1:
        pos = y0
        while pos < y1:
            draw.line((x0, pos, x1, min(pos + dash, y1)), fill=fill, width=2)
            pos += dash + gap
    else:
        pos = x0
        while pos < x1:
            draw.line((pos, y0, min(pos + dash, x1), y1), fill=fill, width=2)
            pos += dash + gap


def render_print_sheet(
    board: Image.Image,
    row: int,
    col: int,
    rows: int,
    cols: int,
    x_px: int,
    y_px: int,
    ppi: int,
    sheet_w_in: float,
    sheet_h_in: float,
    margin_in: float,
    overlap_in: float,
) -> Image.Image:
    """Render one sheet: its piece of the board inside the margins, plus glue guides and a label."""
    sheet_w = round(sheet_w_in * ppi)
    sheet_h = round(sheet_h_in * ppi)
    margin = round(margin_in * ppi)
    content_w = round((sheet_w_in - 2 * margin_in) * ppi)
    content_h = round((sheet_h_in - 2 * margin_in) * ppi)
    overlap = round(overlap_in * ppi)

    sheet = Image.new("RGB", (sheet_w, sheet_h), (255, 255, 255))
    piece = board.crop((x_px, y_px, x_px + content_w, y_px + content_h)).convert("RGBA")
    sheet.paste(piece, (margin, margin), piece)

    draw = ImageDraw.Draw(sheet)
    guide = (120, 120, 120)
    # Dashed lines mark where the next sheet's overlap begins, so the seams line up.
    if col < cols - 1:
        x = margin + content_w - overlap
        _dashed_line(draw, x, margin, x, margin + content_h, guide)
    if row < rows - 1:
        y = margin + content_h - overlap
        _dashed_line(draw, margin, y, margin + content_w, y, guide)
    label = f"Row {row + 1}, col {col + 1} of {rows} × {cols}  ·  glue overlap {overlap_in:g} in"
    draw.text((margin, max(0, margin // 3)), label, fill=(90, 90, 90))
    return sheet


class ResizePanel(ttk.LabelFrame):
    """Resize the selected images on the board, by typed size or by a percent of their original size."""

    def __init__(
        self,
        master: tk.Misc,
        get_canvas: Callable[[], "PosterCanvas"],
        **kwargs: object,
    ) -> None:
        super().__init__(master, text="Resize selected images", padding=6, **kwargs)
        self._get_canvas = get_canvas
        self._width = tk.StringVar(value="")
        self._height = tk.StringVar(value="")
        self._maintain_aspect = tk.BooleanVar(value=True)
        ttk.Label(self, text="W").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self._width, width=8).grid(row=0, column=1, padx=4)
        ttk.Label(self, text="H").grid(row=0, column=2, sticky="w")
        ttk.Entry(self, textvariable=self._height, width=8).grid(row=0, column=3, padx=4)
        ttk.Checkbutton(
            self,
            text="Keep aspect",
            variable=self._maintain_aspect,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Button(self, text="Apply size", command=self.apply_size).grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0)
        )
        ttk.Label(self, text="Percent of original size").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(8, 2)
        )
        percent_row = ttk.Frame(self)
        percent_row.grid(row=4, column=0, columnspan=4, sticky="ew")
        self._percent_buttons: dict[str, ttk.Button] = {}
        for level in DEFAULT_IMPORTANCE_PERCENT:
            button = ttk.Button(percent_row, text=level)
            button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
            self._percent_buttons[level] = button
        self.refresh_percents()

    def refresh_percents(self) -> None:
        """Show the saved Low, Medium and High percents on the buttons."""
        percents = load_importance_percent()
        for level, button in self._percent_buttons.items():
            percent = percents[level]
            button.configure(text=f"{level} {percent}%", command=lambda p=percent: self.scale_percent(p))

    def _selected_images(self) -> list[ImageLayer]:
        canvas = self._get_canvas()
        return [layer for layer in canvas.selected_layers() if isinstance(layer, ImageLayer)]

    def _refresh_board(self, images: list[ImageLayer]) -> None:
        for layer in images:
            record_resize_percent(layer)
        canvas = self._get_canvas()
        canvas._on_selection_change(canvas.selected_layer())
        canvas.redraw()

    def sync_from_selection(self) -> None:
        """Show the first selected image's size in the W and H fields."""
        images = self._selected_images()
        if images:
            self._width.set(str(round(images[0].width)))
            self._height.set(str(round(images[0].height)))
        else:
            self._width.set("")
            self._height.set("")

    def apply_size(self) -> None:
        """Set each selected image's width. With Keep aspect on, its height follows its own ratio."""
        images = self._selected_images()
        if not images:
            messagebox.showinfo("Resize", "Select one or more images on the board first.")
            return
        try:
            width = int(self._width.get().strip())
            height = None if self._maintain_aspect.get() else int(self._height.get().strip())
        except ValueError:
            messagebox.showerror("Resize", "Width and height must be whole numbers.")
            return
        if width < MIN_LAYER_SIZE or (height is not None and height < MIN_LAYER_SIZE):
            messagebox.showerror("Resize", f"Sizes must be at least {MIN_LAYER_SIZE} px.")
            return
        canvas = self._get_canvas()
        canvas.push_undo_checkpoint()
        for layer in images:
            if height is None:
                ratio = layer.width / layer.height if layer.height > 0 else 1.0
                layer.width = float(width)
                layer.height = max(MIN_LAYER_SIZE, width / ratio)
            else:
                layer.width = float(width)
                layer.height = float(height)
        self._refresh_board(images)
        self.sync_from_selection()

    def scale_percent(self, percent: int) -> None:
        """Scale each selected image to percent of its original (source) pixel size."""
        images = self._selected_images()
        if not images:
            messagebox.showinfo("Resize", "Select one or more images on the board first.")
            return
        canvas = self._get_canvas()
        canvas.push_undo_checkpoint()
        scale = percent / 100
        for layer in images:
            layer.width = max(MIN_LAYER_SIZE, layer.source.width * scale)
            layer.height = max(MIN_LAYER_SIZE, layer.source.height * scale)
        self._refresh_board(images)
        self.sync_from_selection()


class ManifestationBoardApp(tk.Tk):
    """Main manifestation board window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Manifestation Board Studio")
        self.minsize(1100, 720)
        self.geometry("1280x800")
        self._updating_props = False
        self._dark_mode = tk.BooleanVar(value=bool(load_settings().get("dark_mode", False)))
        self._keep_aspect_resize = tk.BooleanVar(value=bool(load_settings().get("keep_aspect_on_resize", True)))
        self._build_layout()
        self.canvas.keep_aspect_on_resize = self._keep_aspect_resize.get()
        self._build_menubar()
        self._install_edit_shortcuts()
        self._apply_theme()

    def _on_keep_aspect_resize_toggled(self) -> None:
        self.canvas.keep_aspect_on_resize = self._keep_aspect_resize.get()
        update_settings(keep_aspect_on_resize=self._keep_aspect_resize.get())

    def _current_palette(self) -> dict[str, str]:
        return THEME_PALETTES["dark" if self._dark_mode.get() else "light"]

    def _on_dark_mode_toggled(self) -> None:
        self._apply_theme()
        update_settings(dark_mode=self._dark_mode.get())

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        if not hasattr(self, "_base_ttk_theme"):
            self._base_ttk_theme = "vista" if "vista" in style.theme_names() else style.theme_use()
        p = self._current_palette()
        if self._dark_mode.get():
            # clam honours colour options; vista/aqua draw their own colours and ignore them.
            style.theme_use("clam")
            _style_dark_ttk(style, p)
        else:
            style.theme_use(self._base_ttk_theme)
        style.configure("Hint.TLabel", foreground=p["hint"], background=p["root"])
        style.configure("Shape.Toolbutton", background=p["button"], foreground=p["fg"], padding=3)
        style.map(
            "Shape.Toolbutton",
            background=[("selected", p["select"]), ("active", p["active"])],
            foreground=[("selected", p["select_fg"])],
        )
        self.configure(bg=p["root"])
        self.option_add("*TCombobox*Listbox.background", p["field"])
        self.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["select_fg"])

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New board…", command=self._new_board)
        file_menu.add_command(label="Import images to board…", command=self._import_to_board)
        file_menu.add_separator()
        file_menu.add_command(label="Export board as image…", command=self._export_board)
        file_menu.add_command(label="Export print tiles (multi-page PDF)…", command=self._export_print_tiles)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)

        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo", command=self._undo, accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=self._redo, accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy", command=self._copy_selected, accelerator="Ctrl+C")
        edit_menu.add_command(label="Cut", command=self._cut_selected, accelerator="Ctrl+X")
        edit_menu.add_command(label="Paste", command=self._paste_clipboard, accelerator="Ctrl+V")
        edit_menu.add_command(label="Select all", command=self.canvas.select_all, accelerator="Ctrl+A")
        edit_menu.add_separator()
        edit_menu.add_command(label="Delete selected", command=self._delete_selected, accelerator="Del")
        edit_menu.add_command(label="Bring forward", command=lambda: self.canvas.move_layer_order(1))
        edit_menu.add_command(label="Send backward", command=lambda: self.canvas.move_layer_order(-1))
        edit_menu.add_command(
            label="Bring to front",
            command=lambda: self.canvas.send_layer_to_front(),
        )
        edit_menu.add_command(
            label="Send to back",
            command=lambda: self.canvas.send_layer_to_back(),
        )
        edit_menu.add_separator()
        align_menu = tk.Menu(edit_menu, tearoff=0)
        edit_menu.add_cascade(label="Align to page", menu=align_menu)
        for label, mode in (
            ("Align left", "left"),
            ("Align right", "right"),
            ("Align top", "top"),
            ("Align bottom", "bottom"),
            ("Center horizontally", "center_h"),
            ("Center vertically", "center_v"),
            ("Center on page", "center"),
        ):
            align_menu.add_command(
                label=label,
                command=lambda m=mode: self._align_selected(m),  # type: ignore[misc]
            )
        layer_align_menu = tk.Menu(edit_menu, tearoff=0)
        edit_menu.add_cascade(label="Align to layer", menu=layer_align_menu)
        for label, mode in (
            ("Align left edges", "left"),
            ("Align right edges", "right"),
            ("Align top edges", "top"),
            ("Align bottom edges", "bottom"),
            ("Center horizontally", "center_h"),
            ("Center vertically", "center_v"),
            ("Center on reference", "center"),
        ):
            layer_align_menu.add_command(
                label=label,
                command=lambda m=mode: self._align_to_reference(m),  # type: ignore[misc]
            )
        edit_menu.add_separator()
        edit_menu.add_command(label="Arrange images on page…", command=self._arrange_images)
        edit_menu.add_command(label="Make collage…", command=self._make_collage)

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Zoom to fit", command=self._zoom_fit)
        view_menu.add_command(label="Zoom in", command=lambda: self._zoom_by(1.15))
        view_menu.add_command(label="Zoom out", command=lambda: self._zoom_by(1 / 1.15))

        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Page size & background…", command=self._page_settings_dialog)
        settings_menu.add_command(label="Resize presets…", command=self._resize_presets_dialog)
        settings_menu.add_separator()
        settings_menu.add_checkbutton(
            label="Keep aspect ratio when resizing images",
            variable=self._keep_aspect_resize,
            command=self._on_keep_aspect_resize_toggled,
        )
        settings_menu.add_checkbutton(
            label="Dark mode",
            variable=self._dark_mode,
            command=self._on_dark_mode_toggled,
        )

    def _text_entry_has_focus(self) -> bool:
        """Return True when a text field should keep standard edit shortcuts."""
        focus = self.focus_get()
        if focus is None:
            return False
        widget_class = focus.winfo_class()
        return widget_class in ("Entry", "TEntry", "Text", "TSpinbox")

    def _install_edit_shortcuts(self) -> None:
        """Bind undo, redo, clipboard, and delete shortcuts for the board."""
        self.bind_all("<Control-z>", self._shortcut_undo)
        self.bind_all("<Control-y>", self._shortcut_redo)
        self.bind_all("<Control-Shift-Z>", self._shortcut_redo)
        self.bind_all("<Control-c>", self._shortcut_copy)
        self.bind_all("<Control-x>", self._shortcut_cut)
        self.bind_all("<Control-v>", self._shortcut_paste)
        self.bind_all("<Delete>", self._shortcut_delete)
        self.bind_all("<Control-a>", self._shortcut_select_all)
        self.bind_all("<Escape>", self._shortcut_deselect)

    def _shortcut_select_all(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self.canvas.select_all()
        return "break"

    def _shortcut_deselect(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        # Leave Escape alone while a modal dialog is open; dialogs use it to close.
        if self._text_entry_has_focus() or self.grab_current() is not None:
            return None
        self.canvas.select_layer(None)
        return "break"

    def _shortcut_undo(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self._undo()
        return "break"

    def _shortcut_redo(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self._redo()
        return "break"

    def _shortcut_copy(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self._copy_selected()
        return "break"

    def _shortcut_cut(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self._cut_selected()
        return "break"

    def _shortcut_paste(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self._paste_clipboard()
        return "break"

    def _shortcut_delete(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self._text_entry_has_focus():
            return None
        self._delete_selected()
        return "break"

    def _build_layout(self) -> None:
        navbar = ttk.Frame(self, padding=(8, 6))
        navbar.pack(fill=tk.X)
        ttk.Label(navbar, text="Manifestation Board", font=("Segoe UI", 12, "bold")).pack(
            side=tk.LEFT,
        )
        ttk.Button(navbar, text="Page settings", command=self._page_settings_dialog).pack(
            side=tk.RIGHT,
            padx=4,
        )
        ttk.Button(navbar, text="Export image", command=self._export_board).pack(side=tk.RIGHT, padx=4)
        ttk.Button(navbar, text="Import images", command=self._import_to_board).pack(side=tk.RIGHT)
        ttk.Button(navbar, text="Arrange images…", command=self._arrange_images).pack(side=tk.RIGHT, padx=4)
        ttk.Button(navbar, text="Make collage…", command=self._make_collage).pack(side=tk.RIGHT, padx=4)

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body, width=200)
        body.add(left, weight=0)

        tools = ttk.LabelFrame(left, text="Tools", padding=8)
        tools.pack(fill=tk.X, pady=(0, 8))
        self._tool_var = tk.StringVar(value="select")
        for label, value in (
            ("Select / move", "select"),
            ("Line", "line"),
            ("Rectangle", "rect"),
            ("Ellipse", "ellipse"),
            ("Fill bucket", "fill"),
            ("Crop image", "crop"),
        ):
            ttk.Radiobutton(
                tools,
                text=label,
                value=value,
                variable=self._tool_var,
                command=self._on_tool_change,
            ).pack(anchor="w", pady=2)

        stroke = ttk.LabelFrame(left, text="Stroke", padding=8)
        stroke.pack(fill=tk.X, pady=(0, 8))
        self._stroke_preview = tk.Label(stroke, text="Color", width=10, relief=tk.GROOVE)
        self._stroke_preview.grid(row=0, column=0, padx=2, pady=2)
        ttk.Button(stroke, text="Pick color…", command=self._pick_stroke).grid(row=0, column=1, padx=2)
        ttk.Label(stroke, text="Width").grid(row=1, column=0, sticky="w", pady=(6, 0))
        width_row = ttk.Frame(stroke)
        width_row.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self._stroke_width_var = tk.IntVar(value=3)
        self._stroke_drag_active = False
        self._stroke_scale = ttk.Scale(
            width_row,
            from_=1,
            to=48,
            orient=tk.HORIZONTAL,
            length=110,
            command=self._on_stroke_scale,
        )
        self._stroke_scale.pack(side=tk.LEFT)
        # One undo step per slider drag, not one per pixel of movement.
        self._stroke_scale.bind("<ButtonRelease-1>", self._end_stroke_drag)
        self._stroke_scale.bind("<KeyRelease>", self._end_stroke_drag)
        stroke_spin = ttk.Spinbox(
            width_row,
            from_=1,
            to=48,
            textvariable=self._stroke_width_var,
            width=4,
            command=self._on_stroke_spin,
        )
        stroke_spin.pack(side=tk.LEFT, padx=(6, 0))
        stroke_spin.bind("<FocusOut>", self._on_stroke_spin)
        stroke_spin.bind("<Return>", self._on_stroke_spin)
        self._stroke_sample = tk.Canvas(
            stroke,
            width=150,
            height=40,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#999999",
        )
        self._stroke_sample.grid(row=2, column=0, columnspan=2, pady=(8, 0))

        fill = ttk.LabelFrame(left, text="Fill", padding=8)
        fill.pack(fill=tk.X, pady=(0, 8))
        self._fill_preview = tk.Label(fill, text="Fill", width=10, relief=tk.GROOVE)
        self._fill_preview.grid(row=0, column=0, padx=2, pady=2)
        ttk.Button(fill, text="Pick fill…", command=self._pick_fill).grid(row=0, column=1, padx=2)
        ttk.Label(fill, text="Fill tolerance").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._fill_tol_var = tk.StringVar(value="32")
        tol_spin = ttk.Spinbox(fill, from_=0, to=128, textvariable=self._fill_tol_var, width=6)
        tol_spin.grid(row=1, column=1, sticky="w", pady=(6, 0))
        tol_spin.bind("<FocusOut>", self._sync_fill_tol)
        tol_spin.bind("<Return>", self._sync_fill_tol)

        resize_panel = ResizePanel(left, get_canvas=lambda: self.canvas)
        self._resize_panel = resize_panel
        resize_panel.pack(fill=tk.X, pady=(0, 8))

        center = ttk.Frame(body)
        body.add(center, weight=1)
        status = ttk.Frame(center)
        status.pack(fill=tk.X, pady=(0, 4))
        self._page_info_var = tk.StringVar()
        ttk.Label(status, textvariable=self._page_info_var).pack(side=tk.LEFT)
        ttk.Label(status, text="Scroll wheel zooms").pack(side=tk.RIGHT)

        self.canvas = PosterCanvas(center, on_selection_change=self._on_selection_changed)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._refresh_stroke_ui()
        self._refresh_page_info()

        right = ttk.Frame(body, width=260)
        body.add(right, weight=0)
        self._props_notebook = ttk.Notebook(right)
        self._props_notebook.pack(fill=tk.BOTH, expand=True)

        page_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(page_tab, text="Page")
        ttk.Label(page_tab, text="Width (px)").grid(row=0, column=0, sticky="w")
        self._page_w_var = tk.StringVar(value=str(self.canvas.page_width))
        ttk.Entry(page_tab, textvariable=self._page_w_var, width=10).grid(row=0, column=1, pady=2)
        ttk.Label(page_tab, text="Height (px)").grid(row=1, column=0, sticky="w")
        self._page_h_var = tk.StringVar(value=str(self.canvas.page_height))
        ttk.Entry(page_tab, textvariable=self._page_h_var, width=10).grid(row=1, column=1, pady=2)
        self._landscape_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            page_tab,
            text="Landscape (width ≥ height)",
            variable=self._landscape_var,
            command=self._apply_page_from_panel,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(page_tab, text="Preset").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._preset_var = tk.StringVar(value="US Letter @ 150 DPI (1275×1650)")
        preset = ttk.Combobox(
            page_tab,
            textvariable=self._preset_var,
            values=list(all_page_presets()),
            state="readonly",
            width=22,
        )
        preset.grid(row=2, column=1, pady=(8, 0), sticky="w")
        preset.bind("<<ComboboxSelected>>", self._apply_preset)
        self._preset_combo = preset
        preset_buttons = ttk.Frame(page_tab)
        preset_buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(preset_buttons, text="Save size as preset…", command=self._save_page_preset).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2)
        )
        ttk.Button(preset_buttons, text="Delete preset", command=self._delete_page_preset).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0)
        )
        ttk.Label(page_tab, text="Background").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self._page_bg_var = tk.StringVar(value=self.canvas.page_bg)
        ttk.Entry(page_tab, textvariable=self._page_bg_var, width=10).grid(row=3, column=1, pady=(8, 0))
        ttk.Button(page_tab, text="Apply page", command=self._apply_page_from_panel).grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )

        image_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(image_tab, text="Image")
        self._img_w_var = tk.StringVar(value="0")
        self._img_h_var = tk.StringVar(value="0")
        self._img_opacity_var = tk.StringVar(value="100")
        self._img_border_var = tk.StringVar(value="0")
        self._img_border_color_var = tk.StringVar(value="#000000")
        ttk.Label(image_tab, text="Width (px)").grid(row=0, column=0, sticky="w")
        ttk.Entry(image_tab, textvariable=self._img_w_var, width=10).grid(row=0, column=1, pady=2)
        ttk.Label(image_tab, text="Height (px)").grid(row=1, column=0, sticky="w")
        ttk.Entry(image_tab, textvariable=self._img_h_var, width=10).grid(row=1, column=1, pady=2)
        ttk.Label(image_tab, text="Opacity %").grid(row=2, column=0, sticky="w")
        ttk.Entry(image_tab, textvariable=self._img_opacity_var, width=10).grid(row=2, column=1, pady=2)
        ttk.Label(image_tab, text="Border px").grid(row=3, column=0, sticky="w")
        ttk.Entry(image_tab, textvariable=self._img_border_var, width=10).grid(row=3, column=1, pady=2)
        ttk.Label(image_tab, text="Border color").grid(row=4, column=0, sticky="w")
        ttk.Entry(image_tab, textvariable=self._img_border_color_var, width=10).grid(
            row=4,
            column=1,
            pady=2,
        )
        self._img_stroke_after_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            image_tab,
            text="Stroke after mask (follows mask edge)",
            variable=self._img_stroke_after_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(image_tab, text="Add mask to image", command=self._add_mask).grid(
            row=6,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(8, 0),
        )
        ttk.Label(
            image_tab,
            text="A mask is its own layer. Select it on the board, drag it over the part of the image to keep, then apply it in the Mask tab.",
            wraplength=220,
            style="Hint.TLabel",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(image_tab, text="Apply to selected", command=self._apply_image_props).grid(
            row=8,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )
        ttk.Button(image_tab, text="Reset to source size", command=self._reset_image_size).grid(
            row=9,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(6, 0),
        )
        order = ttk.LabelFrame(image_tab, text="Layer order", padding=6)
        order.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(order, text="Bring to front", command=self.canvas.send_layer_to_front).pack(
            fill=tk.X,
            pady=2,
        )
        ttk.Button(order, text="Send to back", command=self.canvas.send_layer_to_back).pack(
            fill=tk.X,
            pady=2,
        )
        ttk.Button(order, text="Forward", command=lambda: self.canvas.move_layer_order(1)).pack(
            fill=tk.X,
            pady=2,
        )
        ttk.Button(order, text="Backward", command=lambda: self.canvas.move_layer_order(-1)).pack(
            fill=tk.X,
            pady=2,
        )
        self._align_ref_var = tk.StringVar()
        self._align_ref_id_by_label: dict[str, str] = {}
        self._align_ref_combos: list[ttk.Combobox] = []
        self._build_align_controls(image_tab, grid_row=11)

        shape_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(shape_tab, text="Shape")
        ttk.Label(
            shape_tab,
            text="Select a line or shape, then change its stroke color and width in the Stroke panel on the left. Changes apply immediately.",
            wraplength=220,
        ).pack(anchor="w", pady=(0, 8))
        self._build_align_controls(shape_tab, use_pack=True)

        mask_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(mask_tab, text="Mask")
        self._mask_tab = mask_tab
        self._mask_thumbs = {key: self._make_mask_thumbnail(key) for key in MASK_SHAPE_LABELS}
        self._mask_info_var = tk.StringVar(value="Select a mask on the board.")
        self._mask_shape_var = tk.StringVar(value="circle")
        self._mask_radius_var = tk.DoubleVar(value=32.0)
        self._mask_radius_text = tk.StringVar(value="32 px")
        self._mask_style_version = -1
        self._mask_radius_var.trace_add(
            "write",
            lambda *_: self._mask_radius_text.set(f"{int(self._mask_radius_var.get())} px"),
        )
        self._mask_radius_var.trace_add("write", lambda *_: self._on_mask_style_changed())
        ttk.Label(mask_tab, textvariable=self._mask_info_var, wraplength=220).pack(anchor="w")
        shape_frame = ttk.LabelFrame(mask_tab, text="Shape", padding=4)
        shape_frame.pack(fill=tk.X, pady=(8, 0))
        tile_labels = {
            "none": "None",
            "circle": "Circle",
            "rounded": "Rounded",
            "heart": "Heart",
            "star": "Star",
            "hexagon": "Hexagon",
            "diamond": "Diamond",
            "octagon": "Octagon",
            "triangle": "Triangle",
            "pentagon": "Pentagon",
            "cross": "Cross",
            "arrow": "Arrow",
            "edge_wave": "Wave",
            "edge_zigzag": "Zigzag",
            "edge_scallop": "Scallop",
            "edge_arc": "Arc",
        }
        for index, key in enumerate(MASK_SHAPE_LABELS):
            ttk.Radiobutton(
                shape_frame,
                text=tile_labels[key],
                image=self._mask_thumbs[key],
                compound="top",
                style="Shape.Toolbutton",
                variable=self._mask_shape_var,
                command=self._on_mask_shape_picked,
                value=key,
                width=8,
            ).grid(row=index // 4, column=index % 4, padx=2, pady=2)
        radius_frame = ttk.Frame(mask_tab)
        radius_frame.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(radius_frame, text="Corner radius").pack(side=tk.LEFT)
        ttk.Label(radius_frame, textvariable=self._mask_radius_text, width=6).pack(side=tk.RIGHT)
        ttk.Scale(
            radius_frame,
            from_=0,
            to=200,
            variable=self._mask_radius_var,
            length=120,
        ).pack(side=tk.RIGHT, padx=6)
        edge_frame = ttk.LabelFrame(mask_tab, text="Edge side", padding=4)
        edge_frame.pack(fill=tk.X, pady=(6, 0))
        self._mask_side_var = tk.StringVar(value="bottom")
        self._mask_flip_var = tk.BooleanVar(value=False)
        self._edge_widgets: list[ttk.Widget] = []
        for index, (key, label) in enumerate(EDGE_SIDE_LABELS.items()):
            button = ttk.Radiobutton(
                edge_frame,
                text=label,
                value=key,
                variable=self._mask_side_var,
                command=self._on_edge_option_changed,
            )
            button.grid(row=index // 2, column=index % 2, sticky="w", padx=4, pady=2)
            self._edge_widgets.append(button)
        flip = ttk.Checkbutton(
            edge_frame,
            text="Flip (mirror the cut)",
            variable=self._mask_flip_var,
            command=self._on_edge_option_changed,
        )
        flip.grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 0))
        self._edge_widgets.append(flip)
        self._apply_mask_btn = ttk.Button(mask_tab, text="Apply mask", command=self._apply_mask)
        self._apply_mask_btn.pack(fill=tk.X, pady=(14, 0))
        self._apply_mask_btn.state(["disabled"])
        ttk.Label(
            mask_tab,
            text="Drag the mask into place first. Applying trims the image to the mask and removes the mask layer. Undo restores it.",
            wraplength=220,
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 0))

    def _build_align_controls(
        self,
        parent: ttk.Frame,
        *,
        grid_row: Optional[int] = None,
        use_pack: bool = False,
    ) -> None:
        """Add align-to-page and align-to-layer controls for the current selection."""
        page_align = ttk.LabelFrame(parent, text="Align to page", padding=6)
        if use_pack:
            page_align.pack(fill=tk.X)
        elif grid_row is not None:
            page_align.grid(row=grid_row, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self._add_align_button_grid(page_align, self._align_selected)

        layer_align = ttk.LabelFrame(parent, text="Align to layer", padding=6)
        if use_pack:
            layer_align.pack(fill=tk.X, pady=(12, 0))
        elif grid_row is not None:
            layer_align.grid(row=grid_row + 1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Label(layer_align, text="Reference layer").pack(anchor="w")
        ref_combo = ttk.Combobox(
            layer_align,
            textvariable=self._align_ref_var,
            state="readonly",
            width=24,
        )
        ref_combo.pack(fill=tk.X, pady=(2, 6))
        self._align_ref_combos.append(ref_combo)
        self._add_align_button_grid(layer_align, self._align_to_reference)

    def _add_align_button_grid(
        self,
        parent: ttk.Frame,
        on_align: Callable[[AlignMode], None],
    ) -> None:
        """Shared alignment button layout for page or reference targets."""
        row_top = ttk.Frame(parent)
        row_top.pack(fill=tk.X, pady=2)
        ttk.Button(row_top, text="Top", width=8, command=lambda: on_align("top")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )

        row_mid = ttk.Frame(parent)
        row_mid.pack(fill=tk.X, pady=2)
        ttk.Button(row_mid, text="Left", width=8, command=lambda: on_align("left")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )
        ttk.Button(row_mid, text="Center", width=8, command=lambda: on_align("center")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )
        ttk.Button(row_mid, text="Right", width=8, command=lambda: on_align("right")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )

        row_bottom = ttk.Frame(parent)
        row_bottom.pack(fill=tk.X, pady=2)
        ttk.Button(row_bottom, text="Bottom", width=8, command=lambda: on_align("bottom")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )

        row_axis = ttk.Frame(parent)
        row_axis.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row_axis, text="Center H", command=lambda: on_align("center_h")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )
        ttk.Button(row_axis, text="Center V", command=lambda: on_align("center_v")).pack(
            side=tk.LEFT,
            expand=True,
            fill=tk.X,
            padx=1,
        )

    def _refresh_align_reference_choices(self) -> None:
        """Populate reference layer pickers with every layer except the selection."""
        selected_ids = set(self.canvas.selected_ids)
        labels: list[str] = []
        mapping: dict[str, str] = {}
        for index, layer in enumerate(self.canvas.layers):
            if layer.id in selected_ids:
                continue
            label = layer_display_name(layer, index)
            labels.append(label)
            mapping[label] = layer.id
        self._align_ref_id_by_label = mapping
        for combo in self._align_ref_combos:
            combo["values"] = labels
        current = self._align_ref_var.get()
        if current not in labels:
            self._align_ref_var.set(labels[0] if labels else "")

    def _align_selected(self, mode: AlignMode) -> None:
        """Align the selected layer; show a hint if nothing is selected."""
        if self.canvas.selected_layer() is None:
            messagebox.showinfo("Align", "Select a layer on the board first.")
            return
        self.canvas.align_selected(mode)

    def _align_to_reference(self, mode: AlignMode) -> None:
        """Align the selection to the chosen reference layer."""
        if self.canvas.selected_layer() is None:
            messagebox.showinfo("Align", "Select a layer on the board first.")
            return
        ref_id = self._align_ref_id_by_label.get(self._align_ref_var.get())
        if ref_id is None:
            messagebox.showinfo(
                "Align",
                "Choose a reference layer. You need at least two layers on the board.",
            )
            return
        self.canvas.align_selected_to_layer(mode, ref_id)

    def _arrange_images(self) -> None:
        """Ask for margin and gap, then lay out all image layers on the page."""
        if not any(isinstance(layer, ImageLayer) for layer in self.canvas.layers):
            messagebox.showinfo("Arrange Images", "Add at least one image to the board first.")
            return
        if all(layer.user_placed for layer in self.canvas.layers if isinstance(layer, ImageLayer)):
            messagebox.showinfo(
                "Arrange Images",
                "Every image has been moved by hand, so there is nothing to arrange.",
            )
            return

        settings = load_settings()
        margin_var = tk.StringVar(value=str(settings.get("arrange_margin", ARRANGE_DEFAULT_MARGIN)))
        gap_var = tk.StringVar(value=str(settings.get("arrange_gap", ARRANGE_DEFAULT_GAP)))

        dialog = tk.Toplevel(self)
        dialog.title("Arrange images")
        dialog.configure(bg=self._current_palette()["root"])
        dialog.transient(self)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Margin from page edge (px):").grid(row=0, column=0, sticky="w", pady=4)
        margin_entry = ttk.Entry(frame, textvariable=margin_var, width=10)
        margin_entry.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(frame, text="Gap between images (px):").grid(row=1, column=0, sticky="w", pady=4)
        gap_entry = ttk.Entry(frame, textvariable=gap_var, width=10)
        gap_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)

        def confirm() -> None:
            try:
                margin = int(margin_var.get().strip())
                gap = int(gap_var.get().strip())
            except ValueError:
                messagebox.showerror("Arrange images", "Margin and gap must be whole numbers.", parent=dialog)
                return
            if not 0 <= margin <= ARRANGE_MAX_MARGIN or not 0 <= gap <= ARRANGE_MAX_GAP:
                messagebox.showerror(
                    "Arrange images",
                    f"Margin must be 0–{ARRANGE_MAX_MARGIN} px and gap 0–{ARRANGE_MAX_GAP} px.",
                    parent=dialog,
                )
                return
            if 2 * margin >= min(self.canvas.page_width, self.canvas.page_height):
                messagebox.showerror(
                    "Arrange images",
                    "That margin leaves no room on this page. Use a smaller margin.",
                    parent=dialog,
                )
                return
            update_settings(arrange_margin=margin, arrange_gap=gap)
            dialog.destroy()
            self.canvas.arrange_images(margin=margin, gap=gap)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Arrange", command=confirm).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Return>", lambda _e: confirm())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        margin_entry.focus_set()
        dialog.grab_set()

    def _make_collage(self) -> None:
        """Pick a mask shape, then fill the page with every image."""
        if not any(isinstance(layer, ImageLayer) for layer in self.canvas.layers):
            messagebox.showinfo("Make Collage", "Add at least one image to the board first.")
            return

        keys = list(MASK_SHAPE_LABELS)
        labels = [MASK_SHAPE_LABELS[key] for key in keys]
        settings = load_settings()
        saved = settings.get("collage_shape", "rounded")
        choice_var = tk.StringVar(value=MASK_SHAPE_LABELS[saved if saved in MASK_SHAPE_LABELS else "rounded"])
        overlap_var = tk.StringVar(value=str(settings.get("collage_overlap", COLLAGE_DEFAULT_OVERLAP_PCT)))
        stroke_var = tk.StringVar(value=str(settings.get("collage_stroke", 0)))
        color_var = tk.StringVar(value=str(settings.get("collage_stroke_color", "#000000")))
        corner_var = tk.StringVar(value=str(settings.get("collage_corner", COLLAGE_DEFAULT_CORNER_PCT)))

        dialog = tk.Toplevel(self)
        dialog.title("Make collage")
        dialog.configure(bg=self._current_palette()["root"])
        dialog.transient(self)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Mask shape:").grid(row=0, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(frame, textvariable=choice_var, values=labels, state="readonly", width=24)
        combo.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(frame, text="Max overlap (%):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=0, to=100, textvariable=overlap_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Stroke width (px):").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=0, to=50, textvariable=stroke_var, width=8).grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Stroke color:").grid(row=3, column=0, sticky="w", pady=4)
        color_row = ttk.Frame(frame)
        color_row.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Entry(color_row, textvariable=color_var, width=10).pack(side=tk.LEFT)

        def pick_color() -> None:
            chosen = colorchooser.askcolor(color=color_var.get(), title="Stroke color", parent=dialog)
            if chosen[1]:
                color_var.set(chosen[1])

        ttk.Button(color_row, text="Pick…", command=pick_color).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(frame, text="Corner radius (% of cell):").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=0, to=COLLAGE_MAX_CORNER_PCT, textvariable=corner_var, width=8).grid(
            row=4, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(
            frame,
            text="Every image is cropped to fill the page. Images overlap only where needed\n"
            "to cover the gaps left by the mask, up to the max overlap. Corner radius "
            "applies to the Rounded shape.",
            justify=tk.LEFT,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def confirm() -> None:
            label = choice_var.get()
            shape = keys[labels.index(label)] if label in labels else "rounded"
            try:
                overlap_pct = int(overlap_var.get().strip())
                stroke = int(stroke_var.get().strip())
                corner_pct = int(corner_var.get().strip())
            except ValueError:
                messagebox.showerror(
                    "Make Collage",
                    "Overlap, stroke width and corner radius must be whole numbers.",
                    parent=dialog,
                )
                return
            if not 0 <= corner_pct <= COLLAGE_MAX_CORNER_PCT:
                messagebox.showerror(
                    "Make Collage",
                    f"Corner radius must be 0–{COLLAGE_MAX_CORNER_PCT} % of the cell.",
                    parent=dialog,
                )
                return
            if not 0 <= overlap_pct <= 100 or not 0 <= stroke <= COLLAGE_MAX_STROKE:
                messagebox.showerror(
                    "Make Collage",
                    f"Overlap must be 0–100 % and stroke width 0–{COLLAGE_MAX_STROKE} px.",
                    parent=dialog,
                )
                return
            color = color_var.get().strip()
            if len(color) != 7 or not color.startswith("#") or any(ch not in "0123456789abcdefABCDEF" for ch in color[1:]):
                messagebox.showerror("Make Collage", "Stroke color must look like #rrggbb.", parent=dialog)
                return
            update_settings(
                collage_shape=shape,
                collage_overlap=overlap_pct,
                collage_stroke=stroke,
                collage_stroke_color=color,
                collage_corner=corner_pct,
            )
            dialog.destroy()
            result = self.canvas.make_collage(
                shape,
                max_overlap=overlap_pct / 100,
                corner_fraction=corner_pct / 100,
                stroke_width=stroke,
                stroke_color=color,
            )
            if result is None:
                return
            coverage, overlap = result
            messagebox.showinfo(
                "Make Collage",
                f"Covers {coverage:.0%} of the page. {overlap:.0%} of the covered area overlaps.",
            )

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Make collage", command=confirm).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Return>", lambda _e: confirm())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        combo.focus_set()
        dialog.grab_set()

    def _refresh_page_info(self) -> None:
        count = len(self.canvas.selected_ids)
        selection = f"  ·  Selected: {count}" if count > 1 else ""
        self._page_info_var.set(
            f"Board: {self.canvas.page_width} × {self.canvas.page_height} px  ·  "
            f"Zoom: {int(self.canvas.zoom * 100)}%{selection}",
        )

    def _on_tool_change(self) -> None:
        tool = self._tool_var.get()
        if tool in ("select", "line", "rect", "ellipse", "fill", "crop"):
            self.canvas.set_tool(tool)  # type: ignore[arg-type]

    def _update_color_previews(self) -> None:
        stroke_color, _ = self.canvas.displayed_stroke()
        for preview, color in (
            (self._stroke_preview, stroke_color),
            (self._fill_preview, self.canvas.fill_color),
        ):
            # Keep the swatch label readable whatever colour the user picked.
            r, g, b = self.canvas._hex_to_rgb(color)
            text = "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#ffffff"
            preview.configure(bg=color, fg=text)

    def _refresh_stroke_ui(self) -> None:
        """Show the selected shape's stroke (or the defaults) in the Stroke panel."""
        _, width = self.canvas.displayed_stroke()
        self._stroke_width_var.set(width)
        self._stroke_scale.set(width)
        self._update_color_previews()
        self._draw_stroke_sample()

    def _draw_stroke_sample(self) -> None:
        color, width = self.canvas.displayed_stroke()
        sample = self._stroke_sample
        sample.delete("all")
        sample.create_line(16, 20, 134, 20, fill=color, width=min(width, 32), capstyle=tk.ROUND)

    def _pick_stroke(self) -> None:
        color, _ = self.canvas.displayed_stroke()
        chosen = colorchooser.askcolor(color=color, title="Stroke color")
        if chosen[1]:
            self.canvas.apply_stroke_style(color=chosen[1])
            self._refresh_stroke_ui()

    def _read_stroke_width(self) -> Optional[int]:
        """Return the width typed or spun in the spinbox, clamped, or None if it isn't a number yet."""
        try:
            return max(1, min(48, int(self._stroke_width_var.get())))
        except (ValueError, tk.TclError):
            return None

    def _on_stroke_scale(self, value: str) -> None:
        width = max(1, min(48, round(float(value))))
        self._stroke_width_var.set(width)
        undo = not self._stroke_drag_active
        if self.canvas.apply_stroke_style(width=width, undo=undo):
            # The first real change of a drag records the undo step; later ticks join it.
            self._stroke_drag_active = True
        self._draw_stroke_sample()

    def _end_stroke_drag(self, _event: Optional[tk.Event] = None) -> None:
        self._stroke_drag_active = False

    def _on_stroke_spin(self, _event: Optional[tk.Event] = None) -> None:
        width = self._read_stroke_width()
        if width is None:
            return
        self._stroke_scale.set(width)
        self.canvas.apply_stroke_style(width=width)
        self._draw_stroke_sample()

    def _pick_fill(self) -> None:
        color = colorchooser.askcolor(color=self.canvas.fill_color, title="Fill color")
        if color[1]:
            self.canvas.fill_color = color[1]
            self._update_color_previews()

    def _sync_fill_tol(self, _event: Optional[tk.Event] = None) -> None:
        try:
            self.canvas.fill_tolerance = int(self._fill_tol_var.get())
        except ValueError:
            pass

    def _on_selection_changed(self, layer: Optional[BoardLayer]) -> None:
        self._updating_props = True
        self._resize_panel.sync_from_selection()
        if isinstance(layer, ImageLayer):
            self._props_notebook.select(1)
            self._img_w_var.set(str(int(layer.width)))
            self._img_h_var.set(str(int(layer.height)))
            self._img_opacity_var.set(str(int(layer.opacity * 100)))
            self._img_border_var.set(str(layer.border_width))
            self._img_border_color_var.set(layer.border_color)
            self._img_stroke_after_var.set(layer.stroke_after_mask)
        elif isinstance(layer, MaskLayer):
            self._props_notebook.select(self._mask_tab)
            self._mask_shape_var.set(layer.shape)
            self._mask_radius_var.set(float(layer.corner_radius))
            self._mask_side_var.set(layer.side)
            self._mask_flip_var.set(layer.flipped)
            target = self.canvas.get_layer(layer.target_id)
            name = target.source_path.name if isinstance(target, ImageLayer) and target.source_path else "image"
            ready = layer.positioned
            self._mask_info_var.set(
                f"Mask for {name}. "
                + ("Ready to apply." if ready else "Drag the mask into place to enable Apply."),
            )
            self._apply_mask_btn.state(["!disabled"] if ready else ["disabled"])
        if not isinstance(layer, MaskLayer):
            self._mask_info_var.set("Select a mask on the board.")
            self._apply_mask_btn.state(["disabled"])
        self._update_edge_controls()
        self._updating_props = False
        self._refresh_stroke_ui()
        self._refresh_align_reference_choices()
        self.canvas.redraw()
        self._refresh_page_info()

    def _make_mask_thumbnail(self, shape: MaskShape) -> ImageTk.PhotoImage:
        """Render a small sample picture clipped to the shape, for the shape picker."""
        size = 56
        sample = Image.new("RGB", (size, size), (70, 130, 200))
        ImageDraw.Draw(sample).ellipse((12, 12, size - 12, size - 12), fill=(250, 200, 60))
        clipped = apply_shape_mask(sample, create_shape_mask(size, size, shape, 14))
        backdrop = Image.new("RGBA", (size, size), (120, 120, 120, 255))
        backdrop.alpha_composite(clipped)
        return ImageTk.PhotoImage(backdrop)

    def _add_mask(self) -> None:
        """Create a mask over the selected image, or select the one it already has."""
        layer = self.canvas.selected_layer()
        if not isinstance(layer, ImageLayer):
            messagebox.showinfo("Add mask", "Select an image on the board first.")
            return
        if self.canvas.add_mask_for_image(layer) is None:
            messagebox.showinfo(
                "Add mask",
                "This image already has a mask applied. Right-click the image and choose Edit applied mask.",
            )
            return
        self._props_notebook.select(self._mask_tab)

    def _on_mask_shape_picked(self) -> None:
        """Handle a click on a shape button: add a mask to the selected image, or restyle the selected mask."""
        shape = self._mask_shape_var.get()
        if shape not in MASK_SHAPE_LABELS:
            return
        layer = self.canvas.selected_layer()
        if isinstance(layer, ImageLayer):
            mask = self.canvas.add_mask_for_image(layer, shape)
            if mask is None:
                messagebox.showinfo(
                    "Mask",
                    "This image already has a mask applied. Right-click the image and choose Edit applied mask.",
                )
                return
            if mask.shape != shape:
                self.canvas.push_undo_checkpoint()
                mask.shape = shape
                self.canvas.redraw()
            self._update_edge_controls()
            return
        if isinstance(layer, MaskLayer):
            self._on_mask_style_changed()
            return
        messagebox.showinfo("Mask", "Select an image on the board first.")

    def _update_edge_controls(self) -> None:
        """Enable the Edge side group only while an edge shape mask is selected."""
        layer = self.canvas.selected_layer()
        usable = isinstance(layer, MaskLayer) and layer.shape in EDGE_SHAPES
        for widget in self._edge_widgets:
            widget.state(["!disabled"] if usable else ["disabled"])

    def _on_edge_option_changed(self) -> None:
        """Apply the chosen side or flip to the selected edge mask."""
        if self._updating_props:
            return
        layer = self.canvas.selected_layer()
        if not isinstance(layer, MaskLayer) or layer.shape not in EDGE_SHAPES:
            return
        side = self._mask_side_var.get()
        flipped = bool(self._mask_flip_var.get())
        if side not in EDGE_SIDE_LABELS or (layer.side, layer.flipped) == (side, flipped):
            return
        self.canvas.push_undo_checkpoint()
        layer.side = side
        layer.flipped = flipped
        self.canvas.redraw()

    def _on_mask_style_changed(self) -> None:
        """Apply shape or radius changes from the Mask tab to the selected mask."""
        if self._updating_props:
            return
        layer = self.canvas.selected_layer()
        if not isinstance(layer, MaskLayer):
            return
        shape = self._mask_shape_var.get()
        if shape not in MASK_SHAPE_LABELS:
            return
        try:
            radius = max(0, int(self._mask_radius_var.get()))
        except (ValueError, tk.TclError):
            return
        if layer.shape == shape and layer.corner_radius == radius:
            return
        # One undo step per editing session; a new step starts after any other history change.
        if self._mask_style_version != self.canvas.history_version:
            self.canvas.push_undo_checkpoint()
            self._mask_style_version = self.canvas.history_version
        layer.shape = shape
        layer.corner_radius = radius
        self.canvas.redraw()
        self._update_edge_controls()

    def _apply_mask(self) -> None:
        """Bake the selected mask into its image."""
        layer = self.canvas.selected_layer()
        if not isinstance(layer, MaskLayer):
            messagebox.showinfo("Apply mask", "Select a mask on the board first.")
            return
        if not self.canvas.apply_mask(layer.id):
            messagebox.showinfo(
                "Apply mask",
                "The mask doesn't overlap its image. Drag it over the part of the image to keep.",
            )

    def _apply_image_props(self) -> None:
        layer = self.canvas.selected_layer()
        if not isinstance(layer, ImageLayer):
            messagebox.showinfo("Image", "Select an image on the board first.")
            return
        try:
            w = int(self._img_w_var.get())
            h = int(self._img_h_var.get())
            opacity = int(self._img_opacity_var.get())
            border = int(self._img_border_var.get())
        except ValueError:
            messagebox.showerror("Image", "Numeric fields must be integers.")
            return
        self.canvas.push_undo_checkpoint()
        layer.width = max(MIN_LAYER_SIZE, w)
        layer.height = max(MIN_LAYER_SIZE, h)
        record_resize_percent(layer)
        layer.opacity = clamp(opacity, 0, 100) / 100.0
        layer.border_width = max(0, border)
        layer.border_color = self._img_border_color_var.get()
        layer.stroke_after_mask = self._img_stroke_after_var.get()
        self.canvas.redraw()

    def _reset_image_size(self) -> None:
        layer = self.canvas.selected_layer()
        if not isinstance(layer, ImageLayer):
            return
        self.canvas.push_undo_checkpoint()
        layer.width = float(layer.source.width)
        layer.height = float(layer.source.height)
        record_resize_percent(layer)
        self._on_selection_changed(layer)

    def _apply_page_from_panel(self) -> None:
        try:
            w = int(self._page_w_var.get())
            h = int(self._page_h_var.get())
        except ValueError:
            messagebox.showerror("Page", "Width and height must be integers.")
            return
        if self._landscape_var.get():
            # Landscape keeps the long side horizontal; a portrait preset is turned on its side.
            w, h = max(w, h), min(w, h)
            self._page_w_var.set(str(w))
            self._page_h_var.set(str(h))
        bg = self._page_bg_var.get().strip() or "#ffffff"
        self.canvas.set_page_size(w, h, bg=bg)
        self.canvas.paint_layer = Image.new("RGB", (self.canvas.page_width, self.canvas.page_height), bg)
        self.canvas.redraw()
        self._refresh_page_info()

    def _refresh_preset_choices(self) -> None:
        self._preset_combo.configure(values=list(all_page_presets()))

    def _save_page_preset(self) -> None:
        """Save the Width and Height fields as a named preset in the dropdown."""
        try:
            w = int(self._page_w_var.get())
            h = int(self._page_h_var.get())
        except ValueError:
            messagebox.showerror("Page preset", "Width and height must be integers.")
            return
        if not (MIN_PAGE_SIZE <= w <= MAX_PAGE_SIZE and MIN_PAGE_SIZE <= h <= MAX_PAGE_SIZE):
            messagebox.showerror(
                "Page preset", f"Width and height must be {MIN_PAGE_SIZE}–{MAX_PAGE_SIZE} px."
            )
            return
        name = (simpledialog.askstring("Save page preset", "Name for this page size:", parent=self) or "").strip()
        if not name:
            return
        if name in PAGE_PRESETS:
            messagebox.showerror("Page preset", "That name is used by a built-in preset. Choose another name.")
            return
        custom = load_custom_page_presets()
        if name in custom and not messagebox.askyesno(
            "Page preset", f"Replace the saved preset '{name}'?"
        ):
            return
        custom[name] = (w, h)
        update_settings(custom_page_presets={key: [v[0], v[1]] for key, v in custom.items()})
        self._refresh_preset_choices()
        self._preset_var.set(name)

    def _delete_page_preset(self) -> None:
        """Remove the selected saved preset. Built-in presets stay."""
        name = self._preset_var.get()
        if name in PAGE_PRESETS:
            messagebox.showinfo("Page preset", "Built-in presets can't be deleted.")
            return
        custom = load_custom_page_presets()
        if name not in custom:
            messagebox.showinfo("Page preset", "Select a saved preset in the dropdown to delete it.")
            return
        if not messagebox.askyesno("Page preset", f"Delete the saved preset '{name}'?"):
            return
        del custom[name]
        update_settings(custom_page_presets={key: [v[0], v[1]] for key, v in custom.items()})
        self._refresh_preset_choices()
        self._preset_var.set(next(iter(PAGE_PRESETS)))

    def _apply_preset(self, _event: Optional[tk.Event] = None) -> None:
        preset = all_page_presets().get(self._preset_var.get())
        if preset is None:
            return
        w, h = preset
        self._page_w_var.set(str(w))
        self._page_h_var.set(str(h))
        self._apply_page_from_panel()

    def _resize_presets_dialog(self) -> None:
        """Edit the percents used by the Low, Medium and High resize buttons."""
        current = load_importance_percent()
        dialog = tk.Toplevel(self)
        dialog.title("Resize presets")
        dialog.configure(bg=self._current_palette()["root"])
        dialog.transient(self)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="Percent of the original size used by the Low, Medium and High buttons.",
            wraplength=300,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        fields: dict[str, tk.StringVar] = {}
        for row, level in enumerate(DEFAULT_IMPORTANCE_PERCENT, start=1):
            ttk.Label(frame, text=level).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(current[level]))
            ttk.Spinbox(
                frame,
                from_=IMPORTANCE_PERCENT_MIN,
                to=IMPORTANCE_PERCENT_MAX,
                textvariable=var,
                width=8,
            ).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=4)
            fields[level] = var

        def save() -> None:
            values: dict[str, int] = {}
            for level, var in fields.items():
                try:
                    value = int(var.get().strip())
                except ValueError:
                    messagebox.showerror("Resize presets", "Percents must be whole numbers.", parent=dialog)
                    return
                if not IMPORTANCE_PERCENT_MIN <= value <= IMPORTANCE_PERCENT_MAX:
                    messagebox.showerror(
                        "Resize presets",
                        f"Percents must be {IMPORTANCE_PERCENT_MIN}–{IMPORTANCE_PERCENT_MAX}.",
                        parent=dialog,
                    )
                    return
                values[level] = value
            update_settings(importance_percent=values)
            self._resize_panel.refresh_percents()
            dialog.destroy()

        def reset() -> None:
            for level, var in fields.items():
                var.set(str(DEFAULT_IMPORTANCE_PERCENT[level]))

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="Reset to defaults", command=reset).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=save).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Return>", lambda _e: save())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()

    def _page_settings_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Page settings")
        dialog.configure(bg=self._current_palette()["root"])
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Use the Page tab on the right, or set values here.").pack(anchor="w")
        ttk.Button(frame, text="Close", command=dialog.destroy).pack(pady=(12, 0))

    def _new_board(self) -> None:
        if not messagebox.askyesno("New board", "Clear the current board and start fresh?"):
            return
        self.canvas.layers.clear()
        self.canvas.select_layer(None)
        self.canvas.set_page_size(self.canvas.page_width, self.canvas.page_height)
        self.canvas.clear_history()
        self.canvas.redraw()

    def _import_to_board(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Import images",
            filetypes=[
                ("Images", "*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp;*.tif;*.tiff"),
            ],
        )
        if paths:
            self.canvas.add_images_from_files([Path(p) for p in paths])

    def _export_print_tiles(self) -> None:
        """Split the board into printable sheets and save them as a multi-page PDF."""
        saved = {**PRINT_DEFAULTS, **(load_settings().get("print_tiles") or {})}
        paper_var = tk.StringVar(value=str(saved["paper"]) if saved["paper"] in PRINT_PAPERS else str(PRINT_DEFAULTS["paper"]))
        landscape_var = tk.BooleanVar(value=bool(saved["landscape"]))
        ppi_var = tk.StringVar(value=str(saved["ppi"]))
        overlap_var = tk.StringVar(value=str(saved["overlap"]))
        margin_var = tk.StringVar(value=str(saved["margin"]))

        dialog = tk.Toplevel(self)
        dialog.title("Export print tiles")
        dialog.configure(bg=self._current_palette()["root"])
        dialog.transient(self)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="Splits the board into paper-sized sheets. Print each page at 100% (Actual size),\n"
            "then glue the sheets along the dashed overlap guides.",
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Paper:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame,
            textvariable=paper_var,
            values=list(PRINT_PAPERS),
            state="readonly",
            width=26,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Checkbutton(frame, text="Landscape", variable=landscape_var).grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Pixels per inch:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=PRINT_PPI_MIN, to=PRINT_PPI_MAX, textvariable=ppi_var, width=8).grid(
            row=3, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Glue overlap (in):").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=overlap_var, width=8).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(frame, text="Margin (in):").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=margin_var, width=8).grid(row=5, column=1, sticky="w", padx=(8, 0), pady=4)

        def export() -> None:
            try:
                ppi = int(ppi_var.get().strip())
                overlap = float(overlap_var.get().strip())
                margin = float(margin_var.get().strip())
            except ValueError:
                messagebox.showerror("Print tiles", "Pixels per inch, overlap and margin must be numbers.", parent=dialog)
                return
            if not PRINT_PPI_MIN <= ppi <= PRINT_PPI_MAX:
                messagebox.showerror(
                    "Print tiles", f"Pixels per inch must be {PRINT_PPI_MIN}–{PRINT_PPI_MAX}.", parent=dialog
                )
                return
            if not 0 <= overlap <= 2 or not 0 <= margin <= 1:
                messagebox.showerror("Print tiles", "Overlap must be 0–2 in and margin 0–1 in.", parent=dialog)
                return
            paper = paper_var.get() if paper_var.get() in PRINT_PAPERS else str(PRINT_DEFAULTS["paper"])
            width_in, height_in = PRINT_PAPERS[paper]
            if landscape_var.get():
                width_in, height_in = height_in, width_in
            try:
                rows, cols, tiles = plan_print_tiles(
                    self.canvas.page_width, self.canvas.page_height, ppi, width_in, height_in, margin, overlap
                )
            except ValueError as exc:
                messagebox.showerror("Print tiles", str(exc), parent=dialog)
                return
            sheets = rows * cols
            if sheets > PRINT_MAX_SHEETS:
                messagebox.showerror(
                    "Print tiles",
                    f"That would be {sheets} sheets. The limit is {PRINT_MAX_SHEETS}; "
                    "use a lower pixels-per-inch setting, a larger overlap, or a smaller board.",
                    parent=dialog,
                )
                return
            if sheets > PRINT_CONFIRM_SHEETS and not messagebox.askyesno(
                "Print tiles", f"This will make {sheets} sheets. Continue?", parent=dialog
            ):
                return
            path_str = filedialog.asksaveasfilename(
                parent=dialog,
                title="Save print tiles",
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf")],
            )
            if not path_str:
                return
            update_settings(
                print_tiles={
                    "paper": paper,
                    "landscape": bool(landscape_var.get()),
                    "ppi": ppi,
                    "overlap": overlap,
                    "margin": margin,
                }
            )
            board = self.canvas.render_to_image()
            pages = [
                render_print_sheet(board, r, c, rows, cols, x, y, ppi, width_in, height_in, margin, overlap)
                for r, c, x, y in tiles
            ]
            try:
                pages[0].save(
                    path_str,
                    "PDF",
                    resolution=float(ppi),
                    save_all=True,
                    append_images=pages[1:],
                )
            except OSError as exc:
                messagebox.showerror("Export failed", str(exc), parent=dialog)
                return
            dialog.destroy()
            messagebox.showinfo(
                "Print tiles",
                f"Saved {sheets} sheets ({rows} rows × {cols} columns) to:\n{path_str}\n\n"
                "Print at 100% / Actual size. Turn off Fit to page.",
            )

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Export PDF…", command=export).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()

    def _export_board(self) -> None:
        path_str = filedialog.asksaveasfilename(
            title="Export manifestation board",
            defaultextension=".png",
            filetypes=[
                ("PNG", "*.png"),
                ("JPEG", "*.jpg;*.jpeg"),
                ("WEBP", "*.webp"),
            ],
        )
        if not path_str:
            return
        dest = Path(path_str)
        image = self.canvas.render_to_image()
        suffix = dest.suffix.lower()
        save_kwargs: dict[str, object] = {}
        if suffix in (".jpg", ".jpeg"):
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGB")
            save_kwargs["quality"] = 92
            save_kwargs["optimize"] = True
        elif suffix == ".png":
            save_kwargs["optimize"] = True
        try:
            image.save(dest, **save_kwargs)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Exported", f"Board saved ({self.canvas.page_width}×{self.canvas.page_height} px):\n{path_str}")

    def _delete_selected(self) -> None:
        self.canvas.delete_selected()

    def _undo(self) -> None:
        if not self.canvas.undo():
            self.bell()

    def _redo(self) -> None:
        if not self.canvas.redo():
            self.bell()

    def _copy_selected(self) -> None:
        if not self.canvas.copy_selected():
            self.bell()

    def _cut_selected(self) -> None:
        if not self.canvas.cut_selected():
            self.bell()

    def _paste_clipboard(self) -> None:
        if not self.canvas.paste_clipboard():
            self.bell()

    def _zoom_fit(self) -> None:
        self.canvas._fit_zoom()
        self.canvas.redraw()
        self._refresh_page_info()

    def _zoom_by(self, factor: float) -> None:
        self.canvas.zoom = clamp(self.canvas.zoom * factor, 0.05, 4.0)
        self.canvas.redraw()
        self._refresh_page_info()


def main() -> None:
    """Entry point."""
    app = ManifestationBoardApp()
    app.mainloop()


if __name__ == "__main__":
    main()

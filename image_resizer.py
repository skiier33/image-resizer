"""Manifestation board editor: resize images, collage on a custom poster, draw, and export."""

from __future__ import annotations

import base64
import colorsys
import dataclasses
import io
import json
import math
import os
import queue
import random
import threading
import tkinter as tk
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from pathlib import Path
from typing import Callable, Literal, Optional, Union

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageTk
from board_core.common import (
    EDGE_SHAPES,
    EdgeSide,
    MaskShape,
    clamp,
)
from board_core.masks import (
    apply_shape_mask,
    create_shape_mask,
)
from board_core.layout import (
    COLLAGE_DEFAULT_CORNER_PCT,
    COLLAGE_DEFAULT_OVERLAP_PCT,
    COLLAGE_MAX_CORNER_PCT,
    COLLAGE_MAX_STROKE,
    CollageItemOptions,
    aspect_locked_corner_rect,
    compute_arrangement,
    compute_collage,
    compute_shelf_layout,
)

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

def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ. Real environment variables win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if key and value:
            os.environ.setdefault(key, value)


load_dotenv(Path(__file__).resolve().parent / ".env")

SETTINGS_PATH = Path.home() / ".image_resizer_settings.json"


def load_settings() -> dict[str, object]:
    """Return saved app settings, or an empty dict if none can be read."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Page used for a new board and at startup: US Letter at 150 DPI, landscape, unless the user set another.
BUILTIN_DEFAULT_PAGE: tuple[int, int, str] = (1650, 1275, "#ffffff")


def load_default_page() -> tuple[int, int, str]:
    """The default page (width, height, background) saved with Set as default page, or the built-in one."""
    saved = load_settings().get("default_page")
    if not isinstance(saved, dict):
        return BUILTIN_DEFAULT_PAGE
    width, height, bg = saved.get("width"), saved.get("height"), saved.get("bg")
    valid_size = all(
        isinstance(v, int) and not isinstance(v, bool) and MIN_PAGE_SIZE <= v <= MAX_PAGE_SIZE
        for v in (width, height)
    )
    if valid_size and isinstance(bg, str) and _is_hex_color(bg):
        return int(width), int(height), bg  # type: ignore[arg-type]
    return BUILTIN_DEFAULT_PAGE


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
EDGE_SIDE_LABELS: dict[EdgeSide, str] = {"top": "Top", "bottom": "Bottom", "left": "Left", "right": "Right"}

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
    "dollar": "Dollar sign ($)",
    "frame": "Border frame",
}


def is_image_path(path: Path) -> bool:
    """Return True if path has a supported image extension."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def new_id() -> str:
    """Return a unique layer identifier."""
    return uuid.uuid4().hex[:12]


def record_resize_percent(layer: ImageLayer) -> None:
    """Store the layer's displayed size as a percent of its original (pre-collage) pixels."""
    base = layer.collage_source if layer.collage_source is not None else layer.source
    layer.resize_percent = layer.width / max(1, base.width) * 100


def random_stroke_color() -> str:
    """A random, fairly vivid colour as #rrggbb. Saturation and brightness stay high enough to read on a page."""
    r, g, b = colorsys.hsv_to_rgb(random.random(), random.uniform(0.6, 0.95), random.uniform(0.6, 0.95))
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


# Opacity of the unclipped image shown under a mask while that mask is selected.
MASK_GUIDE_OPACITY = 0.35


def _unmasked_pixels(
    base: Image.Image,
    base_rect: tuple[float, float, float, float],
    box: tuple[float, float, float, float],
) -> Image.Image:
    """The part of base that lies under box (page coordinates), with no mask applied.

    base_rect is where base sits on the page. Pixels outside base stay transparent.
    """
    bx, by, bw, bh = base_rect
    x, y, w, h = box
    scale_x = base.width / max(bw, 1e-6)
    scale_y = base.height / max(bh, 1e-6)
    left = round((x - bx) * scale_x)
    top = round((y - by) * scale_y)
    right = max(left + 1, round((x + w - bx) * scale_x))
    bottom = max(top + 1, round((y + h - by) * scale_y))
    return base.convert("RGBA").crop((left, top, right, bottom))


def _is_hex_color(value: str) -> bool:
    return len(value) == 7 and value.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in value[1:])


def _collage_size(layer: ImageLayer) -> tuple[float, float]:
    """The size the image was set to on the board, before any collage. Collage weights use this,
    so an image the user made bigger keeps a bigger share of the page after a re-run."""
    if layer.collage_source is not None and layer.applied_mask is not None:
        return layer.applied_mask.base_w, layer.applied_mask.base_h
    return layer.width, layer.height


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


def _compose_bordered_image(
    layer: ImageLayer,
    masks: Optional[list["MaskLayer"]] = None,
) -> tuple[Image.Image, int, int]:
    """Rasterize an image layer to RGBA, clipped by its masks, and return paste position (ox, oy).

    Border modes:
    - stroke before mask (default): a rectangle around the image, then cut by the masks.
    - stroke after mask: an outline around the visible, masked shape.
    """
    rx, ry, rw, rh = source_page_rect(layer)
    resized = _resized_source(layer.source, (max(1, int(rw)), max(1, int(rh))))
    if layer.opacity < 1.0:
        # The resample is shared with the cache, so change a copy, never the original.
        resized = resized.copy()
        alpha = resized.split()[3]
        alpha = alpha.point(lambda p: int(p * layer.opacity))
        resized.putalpha(alpha)

    mask_list = masks or []
    x, y = int(rx), int(ry)
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


def source_page_rect(layer: ImageLayer) -> tuple[float, float, float, float]:
    """Page rectangle (x, y, w, h) that the layer's pixels cover. Equals the box unless source_rect is set."""
    if layer.source_rect is None:
        return layer.x, layer.y, layer.width, layer.height
    rx, ry, rw, rh = layer.source_rect
    return layer.x + rx, layer.y + ry, rw, rh


def flatten_source_rect(layer: ImageLayer) -> None:
    """Crop the pixels to the layer's box so the box can be resized like any other image.

    Pixels outside the box are dropped. The layer's source changes identity, so Edit applied
    mask no longer offers to restore them.
    """
    if layer.source_rect is None:
        return
    rx, ry, rw, rh = source_page_rect(layer)
    sw, sh = layer.source.width, layer.source.height
    scale_x = sw / rw
    scale_y = sh / rh
    left = clamp(round((layer.x - rx) * scale_x), 0, sw - 1)
    top = clamp(round((layer.y - ry) * scale_y), 0, sh - 1)
    right = clamp(round((layer.x + layer.width - rx) * scale_x), left + 1, sw)
    bottom = clamp(round((layer.y + layer.height - ry) * scale_y), top + 1, sh)
    layer.source = layer.source.crop((left, top, right, bottom))
    layer.source_rect = None


def _shift_l(img: Image.Image, dx: int, dy: int) -> Image.Image:
    """Move an L-mode image by (dx, dy); pixels moved off the canvas are dropped, gaps are 0."""
    out = Image.new("L", img.size, 0)
    out.paste(img, (dx, dy))
    return out


def apply_bevel(img: Image.Image, size: int) -> Image.Image:
    """Light the top-left rim and shade the bottom-right rim of the visible shape."""
    alpha = img.split()[3]
    rgb = img.convert("RGB")
    soft = max(1, size // 3)
    lit = ImageChops.subtract(alpha, _shift_l(alpha, size, size)).filter(ImageFilter.GaussianBlur(soft))
    shaded = ImageChops.subtract(alpha, _shift_l(alpha, -size, -size)).filter(ImageFilter.GaussianBlur(soft))
    rgb = Image.composite(Image.new("RGB", rgb.size, "#ffffff"), rgb, lit.point(lambda v: int(v * 0.5)))
    rgb = Image.composite(Image.new("RGB", rgb.size, "#000000"), rgb, shaded.point(lambda v: int(v * 0.5)))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def apply_emboss(img: Image.Image, strength: float) -> Image.Image:
    """Blend a relief (emboss) of the image's brightness into its colours."""
    alpha = img.split()[3]
    rgb = img.convert("RGB")
    relief = rgb.convert("L").filter(ImageFilter.EMBOSS)
    relief_rgb = Image.merge("RGB", (relief, relief, relief))
    embossed = ImageChops.add(rgb, relief_rgb, 1.0, -128)
    out = Image.blend(rgb, embossed, strength).convert("RGBA")
    out.putalpha(alpha)
    return out


def apply_shadow(
    img: Image.Image,
    offset: int,
    blur: int,
    color: str,
    opacity: float,
) -> tuple[Image.Image, int]:
    """Place a blurred, offset copy of the shape under the image. Returns the canvas and its padding."""
    pad = 3 * blur + abs(offset)
    size = (img.width + pad * 2, img.height + pad * 2)
    padded_alpha = Image.new("L", size, 0)
    padded_alpha.paste(img.split()[3], (pad, pad))
    shadow_alpha = _shift_l(padded_alpha, offset, offset)
    if blur > 0:
        shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(blur))
    shadow_alpha = shadow_alpha.point(lambda v: int(v * opacity))
    shadow = Image.new("RGBA", size, color)
    shadow.putalpha(shadow_alpha)
    top = Image.new("RGBA", size, (0, 0, 0, 0))
    top.paste(img, (pad, pad))
    shadow.alpha_composite(top)
    return shadow, pad


# Resampled RGBA copies of image sources, reused while a layer is moved or its masks change.
# Entries keep the source alive, so an id() key cannot be reused by another image while cached.
_RESIZED_SOURCE_CACHE: "OrderedDict[tuple[int, tuple[int, int]], tuple[Image.Image, Image.Image]]" = OrderedDict()
_RESIZED_SOURCE_LIMIT = 16


def _resized_source(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    """RGBA copy of source at size. The result is shared: callers must not modify it."""
    key = (id(source), size)
    hit = _RESIZED_SOURCE_CACHE.get(key)
    if hit is not None and hit[0] is source:
        _RESIZED_SOURCE_CACHE.move_to_end(key)
        return hit[1]
    resized = source.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    _RESIZED_SOURCE_CACHE[key] = (source, resized)
    while len(_RESIZED_SOURCE_CACHE) > _RESIZED_SOURCE_LIMIT:
        _RESIZED_SOURCE_CACHE.popitem(last=False)
    return resized


def vector_layer_raster(layer: "LineLayer | RectLayer | EllipseLayer") -> tuple[Image.Image, int, int]:
    """Rasterize a line, rectangle or ellipse onto a transparent patch just big enough for it.

    Returns (patch, ox, oy): the patch's top-left corner in page pixels.
    """
    if isinstance(layer, LineLayer):
        xs = (layer.x1, layer.x2)
        ys = (layer.y1, layer.y2)
    else:
        xs = (layer.x1, layer.x2)
        ys = (layer.y1, layer.y2)
    pad = max(1, layer.width) + 2
    left = math.floor(min(xs)) - pad
    top = math.floor(min(ys)) - pad
    right = math.ceil(max(xs)) + pad
    bottom = math.ceil(max(ys)) + pad
    patch = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(patch)
    x1, y1 = layer.x1 - left, layer.y1 - top
    x2, y2 = layer.x2 - left, layer.y2 - top
    if isinstance(layer, LineLayer):
        draw.line((x1, y1, x2, y2), fill=layer.color, width=max(1, layer.width))
    elif isinstance(layer, RectLayer):
        draw.rectangle((x1, y1, x2, y2), outline=layer.outline, fill=layer.fill, width=max(1, layer.width))
    else:
        draw.ellipse((x1, y1, x2, y2), outline=layer.outline, fill=layer.fill, width=max(1, layer.width))
    return patch, left, top


def preview_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Shrink for the on-screen preview: a box reduce by a whole factor, then LANCZOS.

    About 3 to 5 times faster than one LANCZOS pass from full size, with little visible
    difference at preview scale. Export does not use this; it resamples at full quality.
    """
    if size == image.size:
        return image
    factor = max(1, min(image.width // size[0], image.height // size[1]))
    if factor > 1:
        image = image.reduce(factor)
    return image.resize(size, Image.Resampling.LANCZOS)


def compose_image_layer(
    layer: ImageLayer,
    masks: Optional[list["MaskLayer"]] = None,
) -> tuple[Image.Image, int, int]:
    """Rasterize an image layer with its border, masks and effects. Returns (image, ox, oy)."""
    composed, ox, oy = _compose_bordered_image(layer, masks)
    fx = layer.effects
    if fx.bevel and fx.bevel_size > 0:
        composed = apply_bevel(composed, fx.bevel_size)
    if fx.emboss and fx.emboss_strength > 0:
        composed = apply_emboss(composed, fx.emboss_strength)
    if fx.shadow:
        composed, pad = apply_shadow(composed, fx.shadow_offset, fx.shadow_blur, fx.shadow_color, fx.shadow_opacity)
        ox -= pad
        oy -= pad
    return composed, ox, oy


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
        sides=mask.sides,
        flipped=mask.flipped,
        peaks=mask.peaks,
        amplitude=round(mask.amplitude * min(scale_x, scale_y)),
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
            effects=layer.effects,
            source_rect=layer.source_rect,
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
    sides: tuple[EdgeSide, ...] = ("bottom",)
    flipped: bool = False
    peaks: int = 0
    amplitude: int = 0
    # (x, y, w, h) in page coordinates where Edit applied mask should place the base pixels.
    # None: use the base_* rectangle. Make collage sets it so the image keeps its collage size and position.
    restore_rect: Optional[tuple[float, float, float, float]] = None


@dataclass(frozen=True)
class ImageEffects:
    """Optional look-and-feel effects for an image. Frozen so undo snapshots can share it."""

    shadow: bool = False
    shadow_offset: int = 12
    shadow_blur: int = 10
    shadow_color: str = "#000000"
    shadow_opacity: float = 0.6
    bevel: bool = False
    bevel_size: int = 6
    emboss: bool = False
    emboss_strength: float = 0.6


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
    effects: ImageEffects = field(default_factory=ImageEffects)
    # None: the pixels fill the box. Set by Edit applied mask: (x, y, w, h) of the restored pixels,
    # with x and y relative to the box's top-left, so the box stays where the user left it.
    source_rect: Optional[tuple[float, float, float, float]] = None


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
    # Edge shapes cut the chosen sides: which sides, and whether the cuts are mirrored.
    sides: tuple[EdgeSide, ...] = ("bottom",)
    flipped: bool = False
    # Edge shapes: number of waves/teeth/bumps per side (0 = shape default) and their depth in px (0 = default).
    peaks: int = 0
    amplitude: int = 0
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
        # Set by the app: called with the image layer when "Generate with AI…" is chosen.
        self.on_ai_edit: Optional[Callable[[ImageLayer], None]] = None
        # Default page: the one saved with Set as default page, or US Letter at 150 DPI, landscape.
        self.page_width, self.page_height, self.page_bg = load_default_page()
        # Paint is stored behind a property (see paint_layer). Undo snapshots share it, so it is
        # copied before any in-place change (copy-on-write).
        self._paint_version = 0
        self._paint_shared = False
        self._paint: Image.Image = Image.new("RGB", (1, 1))
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
        # (mask id, undo depth before the mask edit began) while a mask is being added or edited.
        self.mask_session: Optional[tuple[str, int]] = None
        self._redo_stack: list[BoardSnapshot] = []
        self._restoring_history = False
        self._history_listener: Optional[Callable[[], None]] = None
        self._clipboard_layers: list[BoardLayer] = []
        self._drag_had_undo = False

        # Retained scene. Each drawable layer is one canvas image, rebuilt only when its raster
        # changes. Drags reposition items and only redraw the overlay. Requests are coalesced
        # into one flush per idle cycle (see redraw).
        self._raster_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._scene_items: dict[tuple[str, str], dict] = {}
        self._paint_item: Optional[int] = None
        self._paint_photo: Optional[ImageTk.PhotoImage] = None
        self._paint_photo_state: Optional[tuple] = None
        self._redraw_job: Optional[str] = None
        self._scene_dirty = True

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
            # Shared, not copied: the paint layer is copied on its next in-place change instead.
            paint_layer=self.paint_layer,
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
            self.paint_layer = snapshot.paint_layer  # the setter marks it shared, so it is never edited in place
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
        self._notify_history()

    def clear_history(self) -> None:
        """Drop undo and redo stacks."""
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._notify_history()

    def set_history_listener(self, listener: Callable[[], None]) -> None:
        """Call listener whenever the undo or redo stacks change."""
        self._history_listener = listener

    def _notify_history(self) -> None:
        if self._history_listener is not None:
            self._history_listener()

    def history_timeline(self) -> tuple[list[str], int]:
        """Return labels for every saved state, oldest first, and the index of the current one."""
        def describe(layers: list[BoardLayer], width: int, height: int) -> str:
            count = len(layers)
            return f"{count} layer{'s' if count != 1 else ''} · {width}×{height} px"

        labels: list[str] = []
        for index, snapshot in enumerate(self._undo_stack):
            name = "Start" if index == 0 else f"Step {index}"
            labels.append(f"{name}: {describe(snapshot.layers, snapshot.page_width, snapshot.page_height)}")
        current = len(labels)
        labels.append(f"Step {current} (current): {describe(self.layers, self.page_width, self.page_height)}")
        for offset, snapshot in enumerate(reversed(self._redo_stack), start=1):
            labels.append(
                f"Step {current + offset}: {describe(snapshot.layers, snapshot.page_width, snapshot.page_height)}"
            )
        return labels, current

    def jump_to_history(self, index: int) -> bool:
        """Move the board to a saved state by undoing or redoing step by step. Returns False if not possible."""
        if self.mask_session is not None:
            return False
        current = len(self._undo_stack)
        if index == current or not 0 <= index <= current + len(self._redo_stack):
            return False
        if index < current:
            for _ in range(current - index):
                self.undo()
        else:
            for _ in range(index - current):
                self.redo()
        return True

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
        self._notify_history()
        return True

    def redo(self) -> bool:
        """Reapply a undone board state."""
        if not self._redo_stack:
            return False
        self._undo_stack.append(self.capture_snapshot())
        snapshot = self._redo_stack.pop()
        self._restore_snapshot(snapshot)
        self._notify_history()
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
        # A mask edit survives clicks on other layers (e.g. the image under the mask);
        # it ends only when the mask is applied, cancelled, or no longer exists.
        if self.mask_session is not None and self.get_layer(self.mask_session[0]) is None:
            self.mask_session = None
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

    def _stroke_targets(self) -> list[BoardLayer]:
        """Selected layers that have a stroke: lines, rectangles, ellipses and images."""
        return [
            layer
            for layer in self.selected_layers()
            if isinstance(layer, (LineLayer, RectLayer, EllipseLayer, ImageLayer))
        ]

    @staticmethod
    def _layer_stroke(layer: BoardLayer) -> tuple[str, int]:
        if isinstance(layer, LineLayer):
            return layer.color, layer.width
        if isinstance(layer, ImageLayer):
            return layer.border_color, layer.border_width
        return layer.outline, layer.width  # type: ignore[union-attr]

    @staticmethod
    def _set_layer_stroke(layer: BoardLayer, color: str, width: int) -> None:
        if isinstance(layer, LineLayer):
            layer.color = color
            layer.width = width
        elif isinstance(layer, ImageLayer):
            layer.border_color = color
            layer.border_width = max(0, width)
            # Follow the mask edge, so the outline matches the visible shape.
            layer.stroke_after_mask = True
        else:
            layer.outline = color  # type: ignore[union-attr]
            layer.width = width  # type: ignore[union-attr]

    def displayed_stroke(self) -> tuple[str, int]:
        """Stroke colour and width of the first selected stroke target, or the defaults when none is selected."""
        targets = self._stroke_targets()
        if not targets:
            return self.stroke_color, self.stroke_width
        color, width = self._layer_stroke(targets[0])
        return color, max(1, width)

    def apply_stroke_style(
        self,
        *,
        color: Optional[str] = None,
        width: Optional[int] = None,
        undo: bool = True,
    ) -> bool:
        """Update the default stroke for new shapes and restyle every selected line, shape and image.

        Returns True when at least one selected layer actually changed.
        """
        if color is not None:
            self.stroke_color = color
        if width is not None:
            self.stroke_width = width
        changes: list[tuple[BoardLayer, str, int]] = []
        for layer in self._stroke_targets():
            cur_color, cur_width = self._layer_stroke(layer)
            new_color = color if color is not None else cur_color
            new_width = width if width is not None else cur_width
            if new_color != cur_color or new_width != cur_width:
                changes.append((layer, new_color, new_width))
        if not changes:
            return False
        if undo:
            self.push_undo_checkpoint()
        for layer, new_color, new_width in changes:
            self._set_layer_stroke(layer, new_color, new_width)
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
            label="Generate with AI…",
            command=lambda: self.on_ai_edit(hit) if self.on_ai_edit else None,
            state="normal" if self.on_ai_edit else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Edit applied mask",
            command=lambda: self.edit_applied_mask(hit.id),
            state="normal" if self.can_edit_applied_mask(hit.id) else "disabled",
        )
        menu.add_command(label="Edit stroke…", command=lambda: self._edit_stroke_dialog(hit))
        menu.add_command(
            label="Fit mask to image",
            command=lambda: self._fit_first_mask(hit.id),
            state="normal" if self.masks_for(hit.id) else "disabled",
        )
        menu.add_command(
            label="Remove mask",
            command=lambda: self.remove_mask(hit.id),
            state="normal" if self.can_remove_mask(hit.id) else "disabled",
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
        resize: bool = True,
    ) -> bool:
        """Position the images not moved by hand so they sit on the page without overlap.

        With resize=True they are also scaled to fill the page; with resize=False they keep
        their current size and are placed in wrapped rows (which may run off the page).
        """
        # Images the user has dragged keep their place and size.
        images = [layer for layer in self.layers if isinstance(layer, ImageLayer) and not layer.user_placed]
        if not images:
            return False
        if not resize:
            sizes = [
                (layer.width + 2 * max(0, layer.border_width), layer.height + 2 * max(0, layer.border_width))
                for layer in images
            ]
            positions, _fits = compute_shelf_layout(sizes, self.page_width, self.page_height, margin, gap)
            self.push_undo_checkpoint()
            for layer, (x, y) in zip(images, positions):
                bw = max(0, layer.border_width)
                layer.x = x + bw
                layer.y = y + bw
            self._on_selection_change(self.selected_layer())
            self.redraw()
            return True
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
            flatten_source_rect(layer)
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
        items: Optional[dict[str, CollageItemOptions]] = None,
    ) -> Optional[tuple[float, float]]:
        """Fill the page with every image, each cropped to its cell and masked.

        shape, stroke_width and stroke_color are the defaults. items gives an image its own
        resize percent, mask shape and stroke; images not listed use the defaults.
        max_overlap is a fraction of covered area. Each stroke is drawn just inside its cell
        and follows the masked shape.
        Returns (coverage, overlap) as fractions, or None when there was nothing to lay out.
        """
        images = [layer for layer in self.layers if isinstance(layer, ImageLayer)]
        if not images:
            return None
        options = [
            (items or {}).get(layer.id)
            or CollageItemOptions(
                percent=100.0,
                shape=shape,
                stroke_width=stroke_width,
                stroke_color=stroke_color,
            )
            for layer in images
        ]
        aspects = [_collage_base(layer).width / max(1, _collage_base(layer).height) for layer in images]
        rects, coverage, overlap = compute_collage(
            aspects,
            self.page_width,
            self.page_height,
            [option.shape for option in options],
            max_overlap=max_overlap,
            corner_fraction=corner_fraction,
            weights=[
                _collage_size(layer)[0] * _collage_size(layer)[1] * option.percent / 100
                for layer, option in zip(images, options)
            ],
        )
        if not rects:
            return None

        self.push_undo_checkpoint()
        # Masks left over from earlier edits no longer match the new layout; the collage replaces them.
        laid_out = {layer.id for layer in images}
        self.layers = [
            layer for layer in self.layers
            if not (isinstance(layer, MaskLayer) and layer.target_id in laid_out)
        ]
        self.set_selection([i for i in self.selected_ids if self.get_layer(i) is not None])
        for layer, (x, y, w, h), option in zip(images, rects, options):
            bw = max(0, int(option.stroke_width))
            item_shape = option.shape
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
                item_shape,
                corner_radius=int(corner_fraction * min(cropped.width, cropped.height)),
            )
            cropped.putalpha(ImageChops.multiply(cropped.split()[3], alpha))
            layer.source = cropped
            layer.source_rect = None
            layer.x, layer.y, layer.width, layer.height = x, y, w, h
            layer.border_width = bw
            layer.border_color = option.stroke_color
            layer.stroke_after_mask = bw > 0
            # Where the full, uncropped pixels sit when the crop fills the box.
            sx = w / cropped.width
            sy = h / cropped.height
            restore_rect = (
                x - ((base.width - cropped.width) // 2) * sx,
                y - ((base.height - cropped.height) // 2) * sy,
                base.width * sx,
                base.height * sy,
            )
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
                shape=item_shape,
                corner_radius=int(corner_fraction * min(w, h)),
                restore_rect=restore_rect,
            )

        self._on_selection_change(self.selected_layer())
        self.redraw()
        return coverage, overlap

    def delete_mask(self, mask_id: str) -> bool:
        """Delete one live mask layer. The image itself is unchanged. Undoable."""
        mask = self.get_layer(mask_id)
        if not isinstance(mask, MaskLayer):
            return False
        self.push_undo_checkpoint()
        self.layers = [layer for layer in self.layers if layer.id != mask_id]
        self.set_selection([i for i in self.selected_ids if self.get_layer(i) is not None])
        self.redraw()
        return True

    def _fit_first_mask(self, image_id: str) -> None:
        """Fit the first mask on an image to that image and select the mask."""
        masks = self.masks_for(image_id)
        if masks and self.fit_mask_to_image(masks[0].id):
            self.set_selection([masks[0].id])

    def fit_mask_to_image(self, mask_id: str) -> bool:
        """Set a mask's position and size to exactly cover its image. Undoable."""
        mask = self.get_layer(mask_id)
        if not isinstance(mask, MaskLayer):
            return False
        image = self.get_layer(mask.target_id)
        if not isinstance(image, ImageLayer):
            return False
        self.push_undo_checkpoint()
        mask.x, mask.y = image.x, image.y
        mask.width, mask.height = image.width, image.height
        mask.positioned = True
        self.redraw()
        return True

    def can_remove_mask(self, image_id: str) -> bool:
        """True when the image has a live mask, or an applied mask that can still be undone."""
        return bool(self.masks_for(image_id)) or self.can_edit_applied_mask(image_id)

    def remove_mask(self, image_id: str) -> bool:
        """Remove the image's mask. A live mask is deleted. For an applied mask the image keeps its
        current position and size and shows its pixels without the mask."""
        image = self.get_layer(image_id)
        if not isinstance(image, ImageLayer):
            return False
        if self.masks_for(image_id):
            self.push_undo_checkpoint()
            self.layers = [
                layer
                for layer in self.layers
                if not (isinstance(layer, MaskLayer) and layer.target_id == image_id)
            ]
            self.set_selection([i for i in self.selected_ids if self.get_layer(i) is not None])
        elif self.can_edit_applied_mask(image_id):
            prior = image.applied_mask
            assert prior is not None
            self.push_undo_checkpoint()
            # Keep the image's current position and size. Only the mask goes: the unmasked pixels
            # are cut from the pre-mask image over the area the mask covered, then shown in the
            # current box.
            # Where the full pre-mask pixels sit: the collage's restore_rect, or the pre-mask box.
            full_rect = prior.restore_rect or (prior.base_x, prior.base_y, prior.base_w, prior.base_h)
            image.source = _unmasked_pixels(
                prior.base_source,
                full_rect,
                (prior.applied_x, prior.applied_y, prior.applied_w, prior.applied_h),
            )
            image.source_rect = None
            image.applied_mask = None
        else:
            return False
        self._on_selection_change(self.selected_layer())
        self.redraw()
        return True

    def cancel_mask_edit(self) -> bool:
        """Leave the mask edit in progress and undo everything it changed. Returns False if none is in progress."""
        if self.mask_session is None:
            return False
        _, depth = self.mask_session
        self.mask_session = None
        while len(self._undo_stack) > depth and self.undo():
            pass
        self.set_selection([])
        self.redraw()
        return True

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
        start_depth = len(self._undo_stack)
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
        self.mask_session = (mask.id, start_depth)
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
        start_depth = len(self._undo_stack)
        self.push_undo_checkpoint()
        # Keep any move made since Apply, so the restored image and mask stay where they were.
        dx = image.x - prior.applied_x
        dy = image.y - prior.applied_y
        # The box stays exactly where it was at click time. The pre-mask pixels are placed
        # relative to it, so they extend past the box instead of moving the box.
        image.source = prior.base_source
        rx, ry, rw, rh = prior.restore_rect or (prior.base_x, prior.base_y, prior.base_w, prior.base_h)
        image.source_rect = (rx - prior.applied_x, ry - prior.applied_y, rw, rh)
        image.applied_mask = None
        # Bring the image to the front so the whole restored image is visible while editing.
        self.layers.remove(image)
        self.layers.append(image)
        mask = MaskLayer(
            target_id=image.id,
            x=prior.mask_x + dx,
            y=prior.mask_y + dy,
            width=prior.mask_w,
            height=prior.mask_h,
            shape=prior.shape,
            corner_radius=prior.corner_radius,
            sides=prior.sides,
            flipped=prior.flipped,
            peaks=prior.peaks,
            amplitude=prior.amplitude,
            positioned=True,
        )
        self.layers.append(mask)
        self.mask_session = (mask.id, start_depth)
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

        # Work in the pixels' own rectangle, which can extend past the box after Edit applied mask.
        px, py, pw, ph = source_page_rect(image)
        ix1 = max(px, mask.x)
        iy1 = max(py, mask.y)
        ix2 = min(px + pw, mask.x + mask.width)
        iy2 = min(py + ph, mask.y + mask.height)
        if ix2 - ix1 < MIN_LAYER_SIZE or iy2 - iy1 < MIN_LAYER_SIZE:
            return False

        sw, sh = image.source.width, image.source.height
        if sw < 1 or sh < 1 or pw < 1 or ph < 1:
            return False
        scale_x = sw / pw
        scale_y = sh / ph
        sx1 = clamp(round((ix1 - px) * scale_x), 0, sw - 1)
        sy1 = clamp(round((iy1 - py) * scale_y), 0, sh - 1)
        sx2 = clamp(round((ix2 - px) * scale_x), sx1 + 1, sw)
        sy2 = clamp(round((iy2 - py) * scale_y), sy1 + 1, sh)

        self.mask_session = None
        cropped = image.source.crop((sx1, sy1, sx2, sy2)).convert("RGBA")
        # Page position of the crop's top-left corner, so the mask lines up with the pixels.
        origin_x = px + sx1 / scale_x
        origin_y = py + sy1 / scale_y
        alpha = mask_alpha(cropped.width, cropped.height, mask, origin_x, origin_y, scale_x, scale_y)
        cropped.putalpha(ImageChops.multiply(cropped.split()[3], alpha))

        self.push_undo_checkpoint()
        base = (image.source, px, py, pw, ph)
        image.source = cropped
        image.source_rect = None
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
            sides=mask.sides,
            flipped=mask.flipped,
            peaks=mask.peaks,
            amplitude=mask.amplitude,
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

        flatten_source_rect(layer)
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

    def render_to_image(self, *, show_mask_guides: bool = False) -> Image.Image:
        """Composite the board at full page resolution. Mask layers clip their image; they draw nothing.

        show_mask_guides is for the on-screen preview only: while a mask is selected, its image
        also appears faintly and unclipped, so the mask can be lined up with it. Exports never use it.
        """
        base = self.paint_layer.convert("RGBA")
        editing_targets = {
            layer.target_id
            for layer in self.selected_layers()
            if isinstance(layer, MaskLayer)
        } if show_mask_guides else set()

        for layer in self.layers:
            if isinstance(layer, ImageLayer):
                if layer.id in editing_targets:
                    ghost = replace(layer, opacity=layer.opacity * MASK_GUIDE_OPACITY)
                    faint, gx, gy = compose_image_layer(ghost, None)
                    base.paste(faint, (gx, gy), faint)
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
            if isinstance(layer, ImageLayer):
                flatten_source_rect(layer)
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
            # Only the outline follows the drag. The pixels are rebuilt once, on release.
            self.redraw(scene=False)
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
        # A resize drag only moved the outline, so the pixels are rebuilt here.
        self.redraw()

    def _flood_fill(self, px: int, py: int) -> None:
        if not (0 <= px < self.page_width and 0 <= py < self.page_height):
            return
        target = self.paint_layer.getpixel((px, py))
        fill_rgb = self._hex_to_rgb(self.fill_color)
        if target == fill_rgb:
            return
        ImageDraw.floodfill(
            self._paint_for_write(),
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

    # The canvas keeps a retained scene. The paint layer and each drawable layer are canvas
    # images, rebuilt only when their pixels change. Overlays (selection, handles, marquee,
    # crop, mask outlines) are recreated on every refresh. Export still uses render_to_image.
    # Page-scale layer rasters kept for reuse. Each can be several MB, so keep this bounded.
    RASTER_CACHE_LIMIT = 48

    @property
    def paint_layer(self) -> Image.Image:
        """The painted layer that sits under all board layers."""
        return self._paint

    @paint_layer.setter
    def paint_layer(self, image: Image.Image) -> None:
        self._paint = image
        # Any paint object may also be held by an undo snapshot, so the next in-place change copies it.
        self._paint_shared = True
        self._paint_version += 1

    def _paint_for_write(self) -> Image.Image:
        """The paint layer, ready for an in-place change. Copies it first if an undo snapshot shares it."""
        if self._paint_shared:
            self._paint = self._paint.copy()
            self._paint_shared = False
        self._paint_version += 1
        return self._paint

    def redraw(self, *, scene: bool = True) -> None:
        """Ask for a refresh. Requests made before the next idle moment are merged into one.

        scene=False refreshes only the overlay, for drags that do not change any pixels yet.
        """
        if scene:
            self._scene_dirty = True
        if self._redraw_job is None:
            self._redraw_job = self.after_idle(self._on_idle_redraw)

    def redraw_now(self) -> None:
        """Refresh the canvas immediately instead of waiting for the idle moment."""
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
            self._redraw_job = None
        if self._scene_dirty:
            self._sync_scene()
            self._scene_dirty = False
        self._draw_overlays()

    def _on_idle_redraw(self) -> None:
        self._redraw_job = None
        self.redraw_now()

    def destroy(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
            self._redraw_job = None
        super().destroy()

    def _sync_scene(self) -> None:
        """Bring the paint layer, layer images and their stacking up to date with the board."""
        ox, oy = self._board_offset()
        zoom = self.zoom
        self._sync_paint(ox, oy, zoom)
        editing = {layer.target_id for layer in self.selected_layers() if isinstance(layer, MaskLayer)}

        order: list[tuple[str, str]] = []  # bottom to top
        for layer in self.layers:
            if isinstance(layer, MaskLayer):
                continue  # masks clip images and draw nothing themselves; their outlines are overlays
            masks = self.masks_for(layer.id) if isinstance(layer, ImageLayer) else None
            if isinstance(layer, ImageLayer) and layer.id in editing:
                # While one of its masks is selected, the image also shows faintly and unclipped.
                ghost = ("ghost", layer.id)
                self._place_scene_item(ghost, self._layer_raster(layer, None, faint=True), ox, oy, zoom)
                order.append(ghost)
            slot = ("layer", layer.id)
            self._place_scene_item(slot, self._layer_raster(layer, masks), ox, oy, zoom)
            order.append(slot)

        live = set(order)
        for slot in [s for s in self._scene_items if s not in live]:
            self.delete(self._scene_items.pop(slot)["item"])
        for slot in order:
            self.tag_raise(self._scene_items[slot]["item"])
        if self._paint_item is not None:
            self.tag_lower(self._paint_item)

    def _sync_paint(self, ox: float, oy: float, zoom: float) -> None:
        paint = self._paint
        shown_size = (max(1, int(self.page_width * zoom)), max(1, int(self.page_height * zoom)))
        state = (paint, self._paint_version, zoom, shown_size)
        prev = self._paint_photo_state
        if prev is None or prev[0] is not paint or prev[1:] != state[1:]:
            shown = preview_resize(paint, shown_size)
            self._paint_photo = ImageTk.PhotoImage(shown)
            self._paint_photo_state = state
            if self._paint_item is None:
                self._paint_item = self.create_image(0, 0, anchor="nw", tags="paint")
            self.itemconfigure(self._paint_item, image=self._paint_photo)
        self.coords(self._paint_item, ox, oy)

    def _place_scene_item(
        self,
        slot: tuple[str, str],
        raster: tuple,
        ox: float,
        oy: float,
        zoom: float,
    ) -> None:
        """Show one layer raster at the zoom level. Its pixels are resized only when they change."""
        key, image, px, py, keep = raster
        rec = self._scene_items.get(slot)
        if rec is None:
            rec = {"item": self.create_image(0, 0, anchor="nw", tags="layer"), "key": None, "zoom": None, "photo": None, "keep": None}
            self._scene_items[slot] = rec
        if rec["key"] != key or rec["zoom"] != zoom:
            # Resize from the page-scale raster straight to its on-screen size.
            shown_size = (max(1, round(image.width * zoom)), max(1, round(image.height * zoom)))
            shown = preview_resize(image, shown_size)
            rec["photo"] = ImageTk.PhotoImage(shown)
            rec["key"] = key
            rec["zoom"] = zoom
            rec["keep"] = keep  # keeps the source alive while the item shows it, so its id stays unique
            self.itemconfigure(rec["item"], image=rec["photo"])
        self.coords(rec["item"], ox + px * zoom, oy + py * zoom)

    def _layer_raster(self, layer: BoardLayer, masks: Optional[list[MaskLayer]], *, faint: bool = False) -> tuple:
        """Page-space raster of one layer as (key, image, x, y, keepalive), cached by what changes its pixels.

        Image layers are composed at the origin and then shifted by whole pixels, so moving a
        layer on its own reuses the cached work unless a mask's position relative to it changed.
        """
        if isinstance(layer, ImageLayer):
            ix, iy = math.floor(layer.x), math.floor(layer.y)
            local_masks = [
                replace(mask, x=float(round(mask.x - ix)), y=float(round(mask.y - iy)))
                for mask in (masks or [])
            ]
            key = (
                "image",
                id(layer.source),
                layer.source_rect,
                layer.width,
                layer.height,
                layer.opacity,
                layer.border_width,
                layer.border_color,
                layer.stroke_after_mask,
                layer.effects,
                faint,
                tuple(
                    (m.x, m.y, m.width, m.height, m.shape, m.corner_radius, m.sides, m.flipped, m.peaks, m.amplitude)
                    for m in local_masks
                ),
            )
            keep: object = layer.source
        else:
            key = (
                type(layer).__name__,
                layer.x1, layer.y1, layer.x2, layer.y2,
                getattr(layer, "color", None), getattr(layer, "outline", None),
                getattr(layer, "fill", None), layer.width,
            )
            keep = None

        # The cache stores each raster's offset from the layer's whole-pixel origin, so a moved
        # layer finds its cached raster and is placed at its current position.
        base_x, base_y = (ix, iy) if isinstance(layer, ImageLayer) else (0, 0)
        cached = self._raster_cache.get(key)
        if cached is not None:
            self._raster_cache.move_to_end(key)
        else:
            if isinstance(layer, ImageLayer):
                # The ghost is drawn unclipped, so it takes no masks and is composed at its faint opacity.
                source = replace(layer, x=0.0, y=0.0, opacity=layer.opacity * (MASK_GUIDE_OPACITY if faint else 1.0))
                image, rel_x, rel_y = compose_image_layer(source, local_masks or None)
            else:
                image, rel_x, rel_y = vector_layer_raster(layer)
            cached = (image, rel_x, rel_y, keep)
            self._raster_cache[key] = cached
            while len(self._raster_cache) > self.RASTER_CACHE_LIMIT:
                self._raster_cache.popitem(last=False)
        image, rel_x, rel_y, keep = cached
        return key, image, base_x + rel_x, base_y + rel_y, keep

    def _draw_overlays(self) -> None:
        """Selection outlines, handles, marquee, crop box and the board border. Always on top."""
        self.delete("overlay")
        ox, oy = self._board_offset()
        disp_w = self.page_width * self.zoom
        disp_h = self.page_height * self.zoom
        self.create_rectangle(
            ox,
            oy,
            ox + disp_w,
            oy + disp_h,
            outline="#666666",
            width=1,
            tags="overlay",
        )

        # Masks are editor-only outlines; they are never drawn into the exported image.
        for mask in self.layers:
            if isinstance(mask, MaskLayer) and mask.id not in self.selected_ids:
                mx1, my1 = self.page_to_canvas(mask.x, mask.y)
                mx2, my2 = self.page_to_canvas(mask.x + mask.width, mask.y + mask.height)
                self.create_rectangle(mx1, my1, mx2, my2, outline=MASK_OUTLINE_COLOR, width=1, dash=(2, 4), tags="overlay")

        for selected in self.selected_layers():
            bx1, by1, bx2, by2 = self._layer_bbox(selected)
            sx1, sy1 = self.page_to_canvas(bx1, by1)
            sx2, sy2 = self.page_to_canvas(bx2, by2)
            highlight = MASK_OUTLINE_COLOR if isinstance(selected, MaskLayer) else "#4a9eff"
            self.create_rectangle(sx1, sy1, sx2, sy2, outline=highlight, width=2, dash=(4, 2), tags="overlay")

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
                    tags="overlay",
                )

        if self._marquee_start is not None and self._marquee_end is not None:
            mx1, my1 = self.page_to_canvas(*self._marquee_start)
            mx2, my2 = self.page_to_canvas(*self._marquee_end)
            self.create_rectangle(mx1, my1, mx2, my2, outline="#4a9eff", width=1, dash=(3, 3), tags="overlay")

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
                tags="overlay",
            )

    def composed_page_image(self) -> Image.Image:
        """The scene composed at page scale, the same way the on-screen scene shows it.

        Used to check the retained scene against render_to_image.
        """
        editing = {layer.target_id for layer in self.selected_layers() if isinstance(layer, MaskLayer)}
        base = self._paint.convert("RGBA")
        for layer in self.layers:
            if isinstance(layer, MaskLayer):
                continue
            parts = []
            if isinstance(layer, ImageLayer):
                if layer.id in editing:
                    parts.append(self._layer_raster(layer, None, faint=True))
                parts.append(self._layer_raster(layer, self.masks_for(layer.id)))
            else:
                parts.append(self._layer_raster(layer, None))
            for _key, image, x, y, _keep in parts:
                base.paste(image, (x, y), image)
        return base


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


# Saved collages: a zip holding manifest.json and one PNG per image.
PROJECT_EXTENSION = ".mbd"
PROJECT_FORMAT = "manifestation-board"
PROJECT_VERSION = 1
def _project_class(name: str) -> Optional[type]:
    """The dataclass with this name in this module, or None. Any board part can be saved and loaded this way."""
    candidate = globals().get(name)
    if isinstance(candidate, type) and dataclasses.is_dataclass(candidate):
        return candidate
    return None


def _encode_project_value(value: object, images: list[tuple[str, Image.Image]], names: dict[int, str]) -> object:
    """Turn a layer field into JSON. Images are written out separately and referenced by name."""
    if isinstance(value, Image.Image):
        key = id(value)
        if key not in names:
            names[key] = f"images/{len(names)}.png"
            images.append((names[key], value))
        return {"__image__": names[key]}
    if isinstance(value, Path):
        return {"__path__": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": type(value).__name__,
            "fields": {
                f.name: _encode_project_value(getattr(value, f.name), images, names)
                for f in dataclasses.fields(value)
            },
        }
    if isinstance(value, tuple):
        return [_encode_project_value(item, images, names) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"A {type(value).__name__} can't be saved in a collage file.")


def _decode_project_value(value: object, archive: zipfile.ZipFile, cache: dict[str, Image.Image]) -> object:
    if isinstance(value, list):
        return tuple(_decode_project_value(item, archive, cache) for item in value)
    if not isinstance(value, dict):
        return value
    if "__image__" in value:
        name = str(value["__image__"])
        if name not in cache:
            with archive.open(name) as handle:
                image = Image.open(io.BytesIO(handle.read()))
                image.load()
                cache[name] = image.copy()
        return cache[name]
    if "__path__" in value:
        return Path(str(value["__path__"]))
    if "__dataclass__" in value:
        name = str(value["__dataclass__"])
        cls = _project_class(name)
        if cls is None or not isinstance(value.get("fields"), dict):
            raise ValueError(f"This collage uses a board part this version doesn't have ({name}).")
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {
            key: _decode_project_value(item, archive, cache)
            for key, item in value["fields"].items()
            if key in known
        }
        if cls in (MaskLayer, AppliedMask) and "sides" not in kwargs and "side" in value["fields"]:
            # Collages saved before multi-side edges stored a single side; keep that cut.
            kwargs["sides"] = (str(value["fields"]["side"]),)
        return cls(**kwargs)
    raise ValueError("The collage file has an entry this version doesn't understand.")


def save_project_file(
    path: Path,
    layers: list[BoardLayer],
    paint_layer: Image.Image,
    page_width: int,
    page_height: int,
    page_bg: str,
) -> None:
    """Write the board to a .mbd file. The file is written beside the target and then moved into place."""
    images: list[tuple[str, Image.Image]] = []
    names: dict[int, str] = {}
    manifest = {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "page": {"width": int(page_width), "height": int(page_height), "bg": page_bg},
        "paint_layer": _encode_project_value(paint_layer, images, names),
        "layers": [_encode_project_value(layer, images, names) for layer in layers],
    }
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, image in images:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            archive.writestr(name, buffer.getvalue())
    os.replace(tmp, path)


def load_project_file(path: Path) -> dict[str, object]:
    """Read a .mbd file. Raises ValueError with a readable message when it isn't a valid collage."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != PROJECT_FORMAT:
            raise ValueError("This file isn't a manifestation board collage.")
        version = manifest.get("version")
        if not isinstance(version, int) or version > PROJECT_VERSION:
            raise ValueError("This collage was saved by a newer version of the app.")
        page = manifest["page"]
        width, height = int(page["width"]), int(page["height"])
        if not (MIN_PAGE_SIZE <= width <= MAX_PAGE_SIZE and MIN_PAGE_SIZE <= height <= MAX_PAGE_SIZE):
            raise ValueError("The page size in this file is out of range.")
        cache: dict[str, Image.Image] = {}
        paint = _decode_project_value(manifest["paint_layer"], archive, cache)
        if not isinstance(paint, Image.Image):
            raise ValueError("The paint layer in this file is damaged.")
        layers = [_decode_project_value(entry, archive, cache) for entry in manifest["layers"]]
    if not all(isinstance(layer, (ImageLayer, MaskLayer, LineLayer, RectLayer, EllipseLayer)) for layer in layers):
        raise ValueError("The layers in this file are damaged.")
    return {
        "page_width": width,
        "page_height": height,
        "page_bg": str(page.get("bg", "#ffffff")),
        "paint_layer": paint.convert("RGB"),
        "layers": layers,
    }


OPENAI_EDIT_URL = "https://api.openai.com/v1/images/edits"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
AI_DEFAULT_MODEL = "gpt-image-1"
# Shown before the list has been refreshed from OpenAI.
AI_FALLBACK_MODELS = ["gpt-image-1", "dall-e-2"]
AI_MAX_INPUT_SIDE = 2048
AI_TIMEOUT_SECONDS = 300


def openai_list_image_models(api_key: str) -> list[str]:
    """Image model ids this API key can use, read from the OpenAI models list.

    Raises RuntimeError with a readable message on any failure.
    """
    request = urllib.request.Request(
        OPENAI_MODELS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"OpenAI error {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Could not reach OpenAI: {exc}") from exc
    try:
        ids = [str(item["id"]) for item in payload["data"]]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected response from OpenAI: {exc}") from exc
    models = sorted(i for i in ids if i.startswith(("gpt-image", "dall-e")))
    if not models:
        raise RuntimeError("This key has no image models listed.")
    return models


def openai_edit_image(api_key: str, model: str, prompt: str, image: Image.Image) -> Image.Image:
    """Send image + prompt to the OpenAI image-edit endpoint and return the generated image.

    Raises RuntimeError with a readable message on any failure.
    """
    src = image.convert("RGBA")
    if max(src.size) > AI_MAX_INPUT_SIDE:
        src.thumbnail((AI_MAX_INPUT_SIDE, AI_MAX_INPUT_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    src.save(buf, format="PNG")

    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in (("model", model), ("prompt", prompt), ("size", "auto")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"),
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="image.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        + buf.getvalue()
        + b"\r\n",
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    request = urllib.request.Request(
        OPENAI_EDIT_URL,
        data=b"".join(parts),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"OpenAI error {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Could not reach OpenAI: {exc}") from exc

    try:
        item = payload["data"][0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        else:
            with urllib.request.urlopen(item["url"], timeout=AI_TIMEOUT_SECONDS) as response:
                raw = response.read()
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        raise RuntimeError(f"Unexpected response from OpenAI: {exc}") from exc


class AIChatPane(ttk.Frame):
    """Foldable chat pane. Each message edits the target image with OpenAI and replaces it."""

    def __init__(
        self,
        master: tk.Misc,
        get_canvas: Callable[[], "PosterCanvas"],
        get_palette: Callable[[], dict[str, str]],
    ) -> None:
        super().__init__(master)
        self._get_canvas = get_canvas
        self._get_palette = get_palette
        self._target_id: Optional[str] = None
        self._busy = False
        self._results: "queue.Queue[tuple[str, object]]" = queue.Queue()
        settings = load_settings()
        self._open = False

        self._toggle_btn = ttk.Button(self, text="▸ AI image chat", command=self.toggle)
        self._toggle_btn.pack(fill=tk.X)

        self._body = ttk.Frame(self, padding=(0, 6, 0, 0))
        saved_key = str(settings.get("openai_api_key") or "")
        self._key_var = tk.StringVar(value=saved_key or os.environ.get("OPENAI_API_KEY", ""))
        self._remember_var = tk.BooleanVar(value=bool(saved_key))
        self._model_var = tk.StringVar(value=str(settings.get("openai_image_model") or AI_DEFAULT_MODEL))
        self._model_results: "queue.Queue[tuple[str, object]]" = queue.Queue()
        saved_models = settings.get("openai_image_models")
        model_choices = [str(m) for m in saved_models] if isinstance(saved_models, list) and saved_models else list(AI_FALLBACK_MODELS)
        if self._model_var.get() not in model_choices:
            model_choices.insert(0, self._model_var.get())
        self._status_var = tk.StringVar(value="Right-click an image > Generate with AI…")

        cfg = ttk.Frame(self._body)
        cfg.pack(fill=tk.X)
        cfg.columnconfigure(1, weight=1)
        ttk.Label(cfg, text="API key").grid(row=0, column=0, sticky="w")
        ttk.Entry(cfg, textvariable=self._key_var, show="•", width=18).grid(row=0, column=1, sticky="ew", pady=1)
        ttk.Label(cfg, text="Model").grid(row=1, column=0, sticky="w")
        self._model_combo = ttk.Combobox(
            cfg,
            textvariable=self._model_var,
            values=model_choices,
            state="readonly",
            width=18,
        )
        self._model_combo.grid(row=1, column=1, sticky="ew", pady=1)
        self._refresh_model_btn = ttk.Button(cfg, text="Refresh", command=self._refresh_models)
        self._refresh_model_btn.grid(row=1, column=2, padx=(4, 0), pady=1)
        ttk.Checkbutton(
            cfg,
            text="Remember key (plain text in settings file)",
            variable=self._remember_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        self._target_label = ttk.Label(self._body, text="Target: none", style="Hint.TLabel")
        self._target_label.pack(anchor="w", pady=(6, 2))

        log_frame = ttk.Frame(self._body)
        log_frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._log = tk.Text(
            log_frame,
            height=9,
            width=28,
            wrap=tk.WORD,
            state=tk.DISABLED,
            yscrollcommand=scroll.set,
            borderwidth=1,
            relief=tk.SOLID,
        )
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=self._log.yview)

        self._prompt = tk.Text(self._body, height=3, width=28, wrap=tk.WORD, borderwidth=1, relief=tk.SOLID)
        self._prompt.pack(fill=tk.X, pady=(6, 0))
        self._prompt.bind("<Return>", self._on_return)
        self._send_btn = ttk.Button(self._body, text="Generate & replace", command=self.send)
        self._send_btn.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(self._body, textvariable=self._status_var, style="Hint.TLabel", wraplength=220).pack(
            anchor="w", pady=(4, 0),
        )
        self.apply_theme()

    def apply_theme(self) -> None:
        p = self._get_palette()
        for widget in (self._log, self._prompt):
            widget.configure(
                bg=p["field"], fg=p["fg"], insertbackground=p["fg"], highlightthickness=0,
            )

    def toggle(self) -> None:
        self.set_open(not self._open)

    def set_open(self, value: bool) -> None:
        self._open = value
        self._toggle_btn.configure(text=("▾ " if value else "▸ ") + "AI image chat")
        if value:
            self._body.pack(fill=tk.BOTH, expand=True)
        else:
            self._body.pack_forget()

    def set_target(self, layer: ImageLayer) -> None:
        """Point the chat at an image, unfold the pane and focus the prompt."""
        if layer.id != self._target_id:
            self._append("— New target image —")
        self._target_id = layer.id
        name = layer.source_path.name if layer.source_path else "image"
        self._target_label.configure(text=f"Target: {name} ({int(layer.width)}×{int(layer.height)})")
        self.set_open(True)
        self._prompt.focus_set()

    def _append(self, text: str) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _on_return(self, event: tk.Event) -> Optional[str]:
        if event.state & SHIFT_MASK:
            return None  # Shift+Enter inserts a newline
        self.send()
        return "break"

    def _target_layer(self) -> Optional[ImageLayer]:
        for layer in self._get_canvas().layers:
            if layer.id == self._target_id and isinstance(layer, ImageLayer):
                return layer
        return None

    def send(self) -> None:
        if self._busy:
            return
        prompt = self._prompt.get("1.0", tk.END).strip()
        key = self._key_var.get().strip()
        layer = self._target_layer()
        if layer is None:
            self._status_var.set("Pick a target: right-click an image > Generate with AI…")
            return
        if not key:
            self._status_var.set("Enter your OpenAI API key.")
            return
        if not prompt:
            return
        model = self._model_var.get().strip() or AI_DEFAULT_MODEL
        update_settings(
            openai_api_key=key if self._remember_var.get() else "",
            openai_image_model=model,
        )
        self._append(f"You: {prompt}")
        self._prompt.delete("1.0", tk.END)
        self._busy = True
        self._send_btn.state(["disabled"])
        self._status_var.set("Generating… this can take a minute.")
        source = layer.source.copy()
        layer_id = layer.id

        def work() -> None:
            try:
                self._results.put(("ok", (layer_id, openai_edit_image(key, model, prompt, source))))
            except Exception as exc:  # shown to the user
                self._results.put(("err", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(150, self._poll)

    def _refresh_models(self) -> None:
        """Fetch the image models this key can use, in the background, and fill the Model dropdown."""
        key = self._key_var.get().strip()
        if not key:
            self._status_var.set("Enter your OpenAI API key first.")
            return
        self._refresh_model_btn.state(["disabled"])
        self._status_var.set("Loading models…")

        def work() -> None:
            try:
                self._model_results.put(("ok", openai_list_image_models(key)))
            except Exception as exc:  # shown to the user
                self._model_results.put(("err", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(150, self._poll_models)

    def _poll_models(self) -> None:
        try:
            kind, data = self._model_results.get_nowait()
        except queue.Empty:
            self.after(150, self._poll_models)
            return
        self._refresh_model_btn.state(["!disabled"])
        if kind == "err":
            self._status_var.set(f"Could not load models: {data}")
            return
        models = [str(m) for m in data]  # type: ignore[union-attr]
        self._model_combo.configure(values=models)
        if self._model_var.get() not in models:
            self._model_var.set(models[0])
        update_settings(openai_image_models=models, openai_image_model=self._model_var.get())
        self._status_var.set(f"Loaded {len(models)} image model(s).")

    def _poll(self) -> None:
        try:
            kind, data = self._results.get_nowait()
        except queue.Empty:
            self.after(150, self._poll)
            return
        self._busy = False
        self._send_btn.state(["!disabled"])
        if kind == "err":
            self._append(f"Error: {data}")
            self._status_var.set("Failed. Your image was not changed.")
            return
        layer_id, image = data  # type: ignore[misc]
        canvas = self._get_canvas()
        layer = next((l for l in canvas.layers if l.id == layer_id and isinstance(l, ImageLayer)), None)
        if layer is None:
            self._append("The target image was removed; result discarded.")
            self._status_var.set("Target image no longer exists.")
            return
        canvas.push_undo_checkpoint()
        layer.source = image
        layer.source_rect = None
        layer.collage_source = None
        layer.applied_mask = None
        # Keep the left edge, top edge and width; follow the new aspect ratio for height.
        layer.height = max(MIN_LAYER_SIZE, layer.width * image.height / image.width)
        canvas.redraw()
        canvas._on_selection_change(canvas.selected_layer())
        self._append("AI: replaced the image. Undo (Ctrl+Z) restores the original.")
        self._status_var.set("Done. Send another message to refine the new image.")
        self.set_target(layer)


class HistoryPanel(ttk.LabelFrame):
    """Lists every saved board state. Click one to return the board to it."""

    def __init__(
        self,
        master: tk.Misc,
        get_canvas: Callable[[], "PosterCanvas"],
        **kwargs: object,
    ) -> None:
        super().__init__(master, text="History", padding=6, **kwargs)
        self._get_canvas = get_canvas
        frame = ttk.Frame(self)
        frame.pack(fill=tk.X)
        scroll = ttk.Scrollbar(frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._list = tk.Listbox(frame, height=8, width=30, exportselection=False, yscrollcommand=scroll.set)
        self._list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=self._list.yview)
        self._list.bind("<Button-1>", self._on_click)
        ttk.Label(self, text="Click a step to return to it.", foreground="#555555", wraplength=200).pack(
            anchor="w", pady=(4, 0),
        )

    def refresh(self) -> None:
        """Rebuild the list from the canvas history and mark the current step."""
        labels, current = self._get_canvas().history_timeline()
        self._list.delete(0, tk.END)
        for label in labels:
            self._list.insert(tk.END, label)
        self._list.itemconfig(current, foreground="#0078d7")
        self._list.selection_clear(0, tk.END)
        self._list.selection_set(current)
        self._list.see(current)

    def _on_click(self, event: tk.Event) -> str:
        index = self._list.nearest(event.y)
        bbox = self._list.bbox(index)
        if bbox is None or not bbox[1] <= event.y < bbox[1] + bbox[3]:
            return "break"
        if not self._get_canvas().jump_to_history(index):
            self.bell()
        self.refresh()
        return "break"


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
        reset_row = ttk.Frame(self)
        reset_row.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(reset_row, text="Reset to").pack(side=tk.LEFT)
        self._reset_var = tk.StringVar(value="100% (original)")
        self._reset_combo = ttk.Combobox(reset_row, textvariable=self._reset_var, state="readonly", width=16)
        self._reset_combo.pack(side=tk.LEFT, padx=4)
        ttk.Button(reset_row, text="Reset", command=self.reset_selected).pack(side=tk.LEFT)
        self.refresh_percents()

    def refresh_percents(self) -> None:
        """Show the saved Low, Medium and High percents on the buttons and in the Reset list."""
        percents = load_importance_percent()
        for level, button in self._percent_buttons.items():
            percent = percents[level]
            button.configure(text=f"{level} {percent}%", command=lambda p=percent: self.scale_percent(p))
        self._reset_choices: dict[str, int] = {"100% (original)": 100}
        for level, percent in percents.items():
            self._reset_choices[f"{level} {percent}%"] = percent
        self._reset_combo.configure(values=list(self._reset_choices))
        if self._reset_var.get() not in self._reset_choices:
            self._reset_var.set("100% (original)")

    def reset_selected(self) -> None:
        """Set the selected images to the percent chosen in Reset to (100% or any preset)."""
        self.scale_percent(self._reset_choices.get(self._reset_var.get(), 100))

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
            flatten_source_rect(layer)
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
            flatten_source_rect(layer)
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
        self._project_path: Optional[Path] = None
        self._saved_marker: Optional[tuple[int, int, int]] = None
        self.after_idle(self._mark_saved)
        self.protocol("WM_DELETE_WINDOW", self._exit_app)
        self.bind_all("<Control-o>", lambda _e: self._project_open())
        self.bind_all("<Control-s>", lambda _e: self._project_save())
        self.bind_all("<Control-Shift-S>", lambda _e: self._project_save_as())
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
        if hasattr(self, "_ai_pane"):
            self._ai_pane.apply_theme()
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
        file_menu.add_command(label="Open collage…", command=self._project_open, accelerator="Ctrl+O")
        file_menu.add_command(label="Save collage", command=self._project_save, accelerator="Ctrl+S")
        file_menu.add_command(label="Save collage as…", command=self._project_save_as, accelerator="Ctrl+Shift+S")
        file_menu.add_command(label="Import images to board…", command=self._import_to_board)
        file_menu.add_separator()
        file_menu.add_command(label="Export board as image…", command=self._export_board)
        file_menu.add_command(label="Export print tiles (multi-page PDF)…", command=self._export_print_tiles)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._exit_app)

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
        ttk.Button(navbar, text="Import images", command=self._import_to_board).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(navbar, text="Export image", command=self._export_board).pack(side=tk.LEFT, padx=4)
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

        history_panel = HistoryPanel(left, get_canvas=lambda: self.canvas)
        self._history_panel = history_panel
        history_panel.pack(fill=tk.X, pady=(0, 8))
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
        self.canvas.set_history_listener(self._history_panel.refresh)
        self._history_panel.refresh()
        self._refresh_stroke_ui()
        self._refresh_page_info()

        right = ttk.Frame(body, width=260)
        body.add(right, weight=0)
        # Packed first at the bottom so it keeps its space when unfolded.
        self._ai_pane = AIChatPane(right, get_canvas=lambda: self.canvas, get_palette=self._current_palette)
        self._ai_pane.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.canvas.on_ai_edit = self._ai_pane.set_target
        self._props_notebook = ttk.Notebook(right)
        self._props_notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        page_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(page_tab, text="Page")
        self._page_unit_var = tk.StringVar(value="px")
        self._page_unit_last = "px"
        self._page_dpi_var = tk.StringVar(value="150")
        self._page_w_label = ttk.Label(page_tab, text="Width (px)")
        self._page_w_label.grid(row=0, column=0, sticky="w")
        self._page_w_var = tk.StringVar(value=str(self.canvas.page_width))
        ttk.Entry(page_tab, textvariable=self._page_w_var, width=10).grid(row=0, column=1, pady=2)
        self._page_h_label = ttk.Label(page_tab, text="Height (px)")
        self._page_h_label.grid(row=1, column=0, sticky="w")
        self._page_h_var = tk.StringVar(value=str(self.canvas.page_height))
        ttk.Entry(page_tab, textvariable=self._page_h_var, width=10).grid(row=1, column=1, pady=2)
        ttk.Label(page_tab, text="Units").grid(row=7, column=0, sticky="w", pady=(8, 0))
        unit_row = ttk.Frame(page_tab)
        unit_row.grid(row=7, column=1, sticky="w", pady=(8, 0))
        for label, value in (("Pixels", "px"), ("Inches", "in")):
            ttk.Radiobutton(
                unit_row,
                text=label,
                value=value,
                variable=self._page_unit_var,
                command=self._on_page_unit_changed,
            ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(page_tab, text="DPI (inches)").grid(row=8, column=0, sticky="w")
        ttk.Entry(page_tab, textvariable=self._page_dpi_var, width=10).grid(row=8, column=1, pady=2, sticky="w")
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
        ttk.Button(page_tab, text="Set as default page", command=self._set_default_page).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
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

        effects_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(effects_tab, text="Effects")
        self._fx_shadow_var = tk.BooleanVar(value=False)
        self._fx_shadow_offset_var = tk.StringVar(value="12")
        self._fx_shadow_blur_var = tk.StringVar(value="10")
        self._fx_shadow_opacity_var = tk.StringVar(value="60")
        self._fx_shadow_color_var = tk.StringVar(value="#000000")
        self._fx_bevel_var = tk.BooleanVar(value=False)
        self._fx_bevel_size_var = tk.StringVar(value="6")
        self._fx_emboss_var = tk.BooleanVar(value=False)
        self._fx_emboss_strength_var = tk.StringVar(value="60")
        ttk.Checkbutton(effects_tab, text="Background shadow", variable=self._fx_shadow_var).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(effects_tab, text="Offset px").grid(row=1, column=0, sticky="w")
        ttk.Entry(effects_tab, textvariable=self._fx_shadow_offset_var, width=10).grid(row=1, column=1, pady=2)
        ttk.Label(effects_tab, text="Blur px").grid(row=2, column=0, sticky="w")
        ttk.Entry(effects_tab, textvariable=self._fx_shadow_blur_var, width=10).grid(row=2, column=1, pady=2)
        ttk.Label(effects_tab, text="Opacity %").grid(row=3, column=0, sticky="w")
        ttk.Entry(effects_tab, textvariable=self._fx_shadow_opacity_var, width=10).grid(row=3, column=1, pady=2)
        ttk.Label(effects_tab, text="Shadow color").grid(row=4, column=0, sticky="w")
        ttk.Entry(effects_tab, textvariable=self._fx_shadow_color_var, width=10).grid(row=4, column=1, pady=2)
        ttk.Checkbutton(effects_tab, text="Bevel (light top-left, shade bottom-right)", variable=self._fx_bevel_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Label(effects_tab, text="Bevel size px").grid(row=6, column=0, sticky="w")
        ttk.Entry(effects_tab, textvariable=self._fx_bevel_size_var, width=10).grid(row=6, column=1, pady=2)
        ttk.Checkbutton(effects_tab, text="Emboss", variable=self._fx_emboss_var).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Label(effects_tab, text="Strength %").grid(row=8, column=0, sticky="w")
        ttk.Entry(effects_tab, textvariable=self._fx_emboss_strength_var, width=10).grid(row=8, column=1, pady=2)
        ttk.Button(effects_tab, text="Apply to selected", command=self._apply_effects_props).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(12, 0)
        )
        ttk.Label(
            effects_tab,
            text="The shadow sits outside the image's box, so it is not part of the selection outline.",
            wraplength=220,
            style="Hint.TLabel",
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))

        shape_tab = ttk.Frame(self._props_notebook, padding=8)
        self._props_notebook.add(shape_tab, text="Shape")
        ttk.Label(
            shape_tab,
            text="Select lines, shapes or images, then change their stroke color and width in the Stroke panel on the left. Changes apply to every selected item immediately.",
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
            "dollar": "Dollar $",
            "frame": "Border",
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
        edge_frame = ttk.LabelFrame(mask_tab, text="Edge sides", padding=4)
        edge_frame.pack(fill=tk.X, pady=(6, 0))
        self._mask_side_vars: dict[EdgeSide, tk.BooleanVar] = {
            key: tk.BooleanVar(value=key == "bottom") for key in EDGE_SIDE_LABELS
        }
        self._mask_flip_var = tk.BooleanVar(value=False)
        self._edge_widgets: list[ttk.Widget] = []
        for index, (key, label) in enumerate(EDGE_SIDE_LABELS.items()):
            button = ttk.Checkbutton(
                edge_frame,
                text=label,
                variable=self._mask_side_vars[key],
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
        self._mask_peaks_var = tk.StringVar(value="")
        self._mask_amp_var = tk.StringVar(value="")
        for row, (label, var) in enumerate((("Peaks per side", self._mask_peaks_var), ("Peak height (px)", self._mask_amp_var))):
            ttk.Label(edge_frame, text=label).grid(row=3 + row, column=0, sticky="w", padx=4, pady=(4, 0))
            entry = ttk.Spinbox(edge_frame, from_=0, to=500, width=6, textvariable=var, command=self._on_edge_option_changed)
            entry.grid(row=3 + row, column=1, sticky="w", padx=4, pady=(4, 0))
            entry.bind("<Return>", lambda _e: self._on_edge_option_changed())
            entry.bind("<FocusOut>", lambda _e: self._on_edge_option_changed())
            self._edge_widgets.append(entry)
        ttk.Label(edge_frame, text="0 = default", style="Hint.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="w", padx=4
        )
        self._apply_mask_btn = ttk.Button(mask_tab, text="Apply mask", command=self._apply_mask)
        self._apply_mask_btn.pack(fill=tk.X, pady=(14, 0))
        self._cancel_mask_btn = ttk.Button(mask_tab, text="Cancel mask edit", command=self._cancel_mask_edit)
        self._cancel_mask_btn.pack(fill=tk.X, pady=(4, 0))
        self._cancel_mask_btn.state(["disabled"])
        ttk.Button(mask_tab, text="Fit to image", command=self._fit_mask_to_image).pack(fill=tk.X, pady=(4, 0))
        ttk.Button(mask_tab, text="Remove mask", command=self._remove_selected_mask).pack(fill=tk.X, pady=(4, 0))
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
        resize_var = tk.BooleanVar(value=bool(settings.get("arrange_resize", True)))
        ttk.Checkbutton(
            frame,
            text="Resize images to fill the page",
            variable=resize_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(
            frame,
            text="Off: images keep their current size and are placed in rows.",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w")
        overlap_on_var = tk.BooleanVar(value=bool(settings.get("arrange_allow_overlap", False)))
        overlap_var = tk.StringVar(value=str(settings.get("arrange_overlap", 20)))
        overlap_entry = ttk.Entry(frame, textvariable=overlap_var, width=10)
        overlap_entry.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=4)
        overlap_entry.state(["!disabled"] if overlap_on_var.get() else ["disabled"])

        def toggle_overlap() -> None:
            overlap_entry.state(["!disabled"] if overlap_on_var.get() else ["disabled"])

        ttk.Checkbutton(
            frame,
            text="Allow overlap",
            variable=overlap_on_var,
            command=toggle_overlap,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frame, text="Overlap (px):").grid(row=5, column=0, sticky="w", pady=4)

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
            layout_gap = gap
            if overlap_on_var.get():
                try:
                    overlap = int(overlap_var.get().strip())
                except ValueError:
                    messagebox.showerror("Arrange images", "Overlap must be a whole number.", parent=dialog)
                    return
                if not 0 <= overlap <= ARRANGE_MAX_GAP:
                    messagebox.showerror(
                        "Arrange images", f"Overlap must be 0–{ARRANGE_MAX_GAP} px.", parent=dialog
                    )
                    return
                # A negative gap makes neighbouring images overlap by that many pixels.
                layout_gap = -overlap
            if 2 * margin >= min(self.canvas.page_width, self.canvas.page_height):
                messagebox.showerror(
                    "Arrange images",
                    "That margin leaves no room on this page. Use a smaller margin.",
                    parent=dialog,
                )
                return
            resize = resize_var.get()
            if not resize:
                movable = [l for l in self.canvas.layers if isinstance(l, ImageLayer) and not l.user_placed]
                sizes = [
                    (l.width + 2 * max(0, l.border_width), l.height + 2 * max(0, l.border_width)) for l in movable
                ]
                _pos, fits = compute_shelf_layout(
                    sizes, self.canvas.page_width, self.canvas.page_height, margin, layout_gap,
                )
                if not fits and not messagebox.askyesno(
                    "Arrange images",
                    "At their current sizes the images don't fit on the page. Arrange anyway?",
                    parent=dialog,
                ):
                    return
            update_settings(
                arrange_margin=margin,
                arrange_gap=gap,
                arrange_resize=resize,
                arrange_allow_overlap=bool(overlap_on_var.get()),
                arrange_overlap=overlap_var.get().strip(),
            )
            dialog.destroy()
            self.canvas.arrange_images(margin=margin, gap=layout_gap, resize=resize)

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Arrange", command=confirm).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Return>", lambda _e: confirm())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        margin_entry.focus_set()
        dialog.grab_set()

    def _make_collage(self) -> None:
        """Choose the defaults, then set resize, mask and stroke for each image before filling the page."""
        images = [layer for layer in self.canvas.layers if isinstance(layer, ImageLayer)]
        if not images:
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

        ttk.Label(frame, text="Default mask shape:").grid(row=0, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(frame, textvariable=choice_var, values=labels, state="readonly", width=24)
        combo.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Label(frame, text="Max overlap (%):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=0, to=100, textvariable=overlap_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Default stroke width (px):").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=0, to=COLLAGE_MAX_STROKE, textvariable=stroke_var, width=8).grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Default stroke color:").grid(row=3, column=0, sticky="w", pady=4)
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

        # Per-image list: thumbnail, resize %, mask, stroke width and colour.
        rows: list[dict[str, object]] = []
        thumbs: list[ImageTk.PhotoImage] = []

        def pick_row_color(var: tk.StringVar) -> None:
            chosen = colorchooser.askcolor(color=var.get(), title="Stroke color", parent=dialog)
            if chosen[1]:
                var.set(chosen[1])

        def copy_defaults() -> None:
            for row in rows:
                row["shape"].set(choice_var.get())  # type: ignore[union-attr]
                row["stroke"].set(stroke_var.get())  # type: ignore[union-attr]
                row["color"].set(color_var.get())  # type: ignore[union-attr]

        def randomize_colors() -> None:
            for row in rows:
                row["color"].set(random_stroke_color())  # type: ignore[union-attr]

        def randomize_masks() -> None:
            # Every shape except "None", so each image gets a visible mask.
            choices = [MASK_SHAPE_LABELS[key] for key in MASK_SHAPE_LABELS if key != "none"]
            for row in rows:
                row["shape"].set(random.choice(choices))  # type: ignore[union-attr]

        row_buttons = ttk.Frame(frame)
        row_buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(row_buttons, text="Copy defaults to every image", command=copy_defaults).pack(side=tk.LEFT)
        ttk.Button(row_buttons, text="Randomize colors", command=randomize_colors).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row_buttons, text="Randomize masks", command=randomize_masks).pack(side=tk.LEFT, padx=(6, 0))

        presets: dict[str, float] = {
            "100% (original)": 100.0,
            **{f"{level} {percent}%": float(percent) for level, percent in load_importance_percent().items()},
        }

        def preset_label(value: Optional[float]) -> str:
            """The preset whose percent matches value, or Custom."""
            for label, percent in presets.items():
                if value is not None and abs(percent - value) < 1e-6:
                    return label
            return "Custom"

        def parse_percent(text: str) -> Optional[float]:
            try:
                return float(text.strip())
            except ValueError:
                return None

        def selected_rows() -> list[dict[str, object]]:
            return [row for row in rows if row["select"].get()]  # type: ignore[union-attr]

        def set_all_selected(flag: bool) -> None:
            for row in rows:
                row["select"].set(flag)  # type: ignore[union-attr]

        def apply_bulk() -> None:
            targets = selected_rows()
            if not targets:
                messagebox.showinfo("Make Collage", "Tick the Select box on one or more rows first.", parent=dialog)
                return
            percent = parse_percent(bulk_percent.get())
            try:
                stroke_value = int(bulk_stroke.get().strip())
            except ValueError:
                messagebox.showerror("Make Collage", "Stroke px must be a whole number.", parent=dialog)
                return
            color_value = bulk_color.get().strip()
            if percent is None or not 1 <= percent <= 400:
                messagebox.showerror("Make Collage", "Size % must be a number from 1 to 400.", parent=dialog)
                return
            if not 0 <= stroke_value <= COLLAGE_MAX_STROKE:
                messagebox.showerror("Make Collage", f"Stroke px must be 0–{COLLAGE_MAX_STROKE}.", parent=dialog)
                return
            if not _is_hex_color(color_value):
                messagebox.showerror("Make Collage", "Color must look like #rrggbb.", parent=dialog)
                return
            for row in targets:
                row["shape"].set(bulk_shape.get())  # type: ignore[union-attr]
                row["stroke"].set(str(stroke_value))  # type: ignore[union-attr]
                row["color"].set(color_value)  # type: ignore[union-attr]
                row["percent"].set(f"{percent:g}")  # type: ignore[union-attr]

        def randomize_size() -> None:
            targets = selected_rows() or rows
            low = parse_percent(min_var.get())
            high = parse_percent(max_var.get())
            if low is None or high is None or not (1 <= low <= high <= 400):
                messagebox.showerror(
                    "Make Collage", "Random size needs a min and max from 1 to 400, with min not above max.", parent=dialog
                )
                return
            for row in targets:
                row["percent"].set(f"{random.uniform(low, high):.0f}")  # type: ignore[union-attr]

        bulk = ttk.LabelFrame(frame, text="Selected rows", padding=6)
        bulk.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        bulk_shape = tk.StringVar(value=choice_var.get())
        bulk_stroke = tk.StringVar(value=stroke_var.get())
        bulk_color = tk.StringVar(value=color_var.get())
        bulk_percent = tk.StringVar(value="100")
        min_var = tk.StringVar(value="50")
        max_var = tk.StringVar(value="150")
        ttk.Button(bulk, text="Select all", command=lambda: set_all_selected(True)).grid(row=0, column=0, padx=2, pady=2)
        ttk.Button(bulk, text="Clear", command=lambda: set_all_selected(False)).grid(row=0, column=1, padx=2, pady=2)
        ttk.Label(bulk, text="Mask").grid(row=0, column=2, padx=(10, 2))
        ttk.Combobox(bulk, textvariable=bulk_shape, values=labels, state="readonly", width=15).grid(
            row=0, column=3, padx=2
        )
        ttk.Label(bulk, text="Stroke px").grid(row=0, column=4, padx=(10, 2))
        ttk.Spinbox(bulk, from_=0, to=COLLAGE_MAX_STROKE, textvariable=bulk_stroke, width=5).grid(
            row=0, column=5, padx=2
        )
        ttk.Label(bulk, text="Color").grid(row=0, column=6, padx=(10, 2))
        ttk.Entry(bulk, textvariable=bulk_color, width=9).grid(row=0, column=7, padx=2)
        ttk.Label(bulk, text="Size %").grid(row=0, column=8, padx=(10, 2))
        ttk.Entry(bulk, textvariable=bulk_percent, width=6).grid(row=0, column=9, padx=2)
        ttk.Button(bulk, text="Apply to selected", command=apply_bulk).grid(row=0, column=10, padx=(10, 2))
        ttk.Label(bulk, text="Random size: min %").grid(row=1, column=2, sticky="e", pady=(6, 0))
        ttk.Entry(bulk, textvariable=min_var, width=6).grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Label(bulk, text="max %").grid(row=1, column=4, sticky="e", pady=(6, 0))
        ttk.Entry(bulk, textvariable=max_var, width=6).grid(row=1, column=5, sticky="w", pady=(6, 0))
        ttk.Button(bulk, text="Randomize size", command=randomize_size).grid(
            row=1, column=6, columnspan=2, sticky="w", padx=(10, 2), pady=(6, 0)
        )
        ttk.Label(bulk, text="Randomizes the ticked rows, or every row if none is ticked.").grid(
            row=1, column=8, columnspan=3, sticky="w", pady=(6, 0)
        )

        box = ttk.LabelFrame(frame, text="Images: select, size % (or preset), mask and stroke", padding=6)
        box.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        list_canvas = tk.Canvas(box, width=780, height=300, highlightthickness=0, bg=self._current_palette()["field"])
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL, command=list_canvas.yview)
        inner = ttk.Frame(list_canvas)
        inner.bind("<Configure>", lambda _e: list_canvas.configure(scrollregion=list_canvas.bbox("all")))
        list_canvas.create_window((0, 0), window=inner, anchor="nw")
        list_canvas.configure(yscrollcommand=scroll.set)
        list_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for column, title in enumerate(("", "", "Image", "Preset", "Size %", "Mask", "Stroke px", "Color", "")):
            ttk.Label(inner, text=title).grid(row=0, column=column, sticky="w", padx=4, pady=(0, 4))
        for index, layer in enumerate(images, start=1):
            select_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(inner, variable=select_var).grid(row=index, column=0, padx=(4, 0))
            thumb = _collage_base(layer).convert("RGBA").copy()
            thumb.thumbnail((56, 56))
            photo = ImageTk.PhotoImage(thumb)
            thumbs.append(photo)
            ttk.Label(inner, image=photo).grid(row=index, column=1, padx=4, pady=3)
            name = layer.source_path.name if layer.source_path else f"Image {index}"
            ttk.Label(inner, text=name, width=18).grid(row=index, column=2, sticky="w", padx=4)

            percent_var = tk.StringVar(value="100")
            preset_var = tk.StringVar(value=preset_label(100.0))
            preset_combo = ttk.Combobox(
                inner,
                textvariable=preset_var,
                values=["Custom", *presets],
                state="readonly",
                width=15,
            )
            preset_combo.grid(row=index, column=3, padx=4)

            def choose_preset(_event: object = None, pv: tk.StringVar = preset_var, pct: tk.StringVar = percent_var) -> None:
                value = presets.get(pv.get())
                if value is not None:
                    pct.set(f"{value:g}")

            preset_combo.bind("<<ComboboxSelected>>", choose_preset)
            ttk.Entry(inner, textvariable=percent_var, width=7).grid(row=index, column=4, padx=4)
            percent_var.trace_add(
                "write",
                lambda *_args, pv=percent_var, lv=preset_var: lv.set(preset_label(parse_percent(pv.get()))),
            )
            shape_var = tk.StringVar(value=choice_var.get())
            ttk.Combobox(inner, textvariable=shape_var, values=labels, state="readonly", width=16).grid(
                row=index, column=5, padx=4
            )
            row_stroke_var = tk.StringVar(value=stroke_var.get())
            ttk.Spinbox(inner, from_=0, to=COLLAGE_MAX_STROKE, textvariable=row_stroke_var, width=5).grid(
                row=index, column=6, padx=4
            )
            row_color_var = tk.StringVar(value=color_var.get())
            ttk.Entry(inner, textvariable=row_color_var, width=9).grid(row=index, column=7, padx=4)
            ttk.Button(inner, text="Pick…", command=lambda v=row_color_var: pick_row_color(v)).grid(
                row=index, column=8, padx=(2, 4)
            )
            rows.append(
                {
                    "layer": layer,
                    "select": select_var,
                    "preset": preset_var,
                    "percent": percent_var,
                    "shape": shape_var,
                    "stroke": row_stroke_var,
                    "color": row_color_var,
                }
            )

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
            if not _is_hex_color(color):
                messagebox.showerror("Make Collage", "Stroke color must look like #rrggbb.", parent=dialog)
                return

            items: dict[str, CollageItemOptions] = {}
            for row in rows:
                name = str(row["layer"].source_path.name if row["layer"].source_path else "an image")  # type: ignore[union-attr]
                try:
                    percent = float(str(row["percent"].get()).strip())  # type: ignore[union-attr]
                    row_stroke = int(str(row["stroke"].get()).strip())  # type: ignore[union-attr]
                except ValueError:
                    messagebox.showerror("Make Collage", f"Check the resize % and stroke for {name}.", parent=dialog)
                    return
                if not 1 <= percent <= 400:
                    messagebox.showerror("Make Collage", f"Resize % for {name} must be 1–400.", parent=dialog)
                    return
                if not 0 <= row_stroke <= COLLAGE_MAX_STROKE:
                    messagebox.showerror(
                        "Make Collage", f"Stroke for {name} must be 0–{COLLAGE_MAX_STROKE} px.", parent=dialog
                    )
                    return
                row_color = str(row["color"].get()).strip()  # type: ignore[union-attr]
                if not _is_hex_color(row_color):
                    messagebox.showerror("Make Collage", f"Color for {name} must look like #rrggbb.", parent=dialog)
                    return
                row_label = str(row["shape"].get())  # type: ignore[union-attr]
                row_shape = keys[labels.index(row_label)] if row_label in labels else shape
                items[row["layer"].id] = CollageItemOptions(  # type: ignore[union-attr]
                    percent=percent,
                    shape=row_shape,
                    stroke_width=row_stroke,
                    stroke_color=row_color,
                )

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
                items=items,
            )
            if result is None:
                return
            coverage, overlap = result
            messagebox.showinfo(
                "Make Collage",
                f"Covers {coverage:.0%} of the page. {overlap:.0%} of the covered area overlaps.",
            )

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Make collage", command=confirm).pack(side=tk.RIGHT, padx=(0, 6))
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
        """Show the selected layer's stroke (or the defaults) in the Stroke panel."""
        _, width = self.canvas.displayed_stroke()
        self._stroke_width_var.set(width)
        # Setting the slider fires its change handler, which would push this width onto the
        # selection. Silence it while the panel just shows the current selection.
        self._stroke_refreshing = True
        try:
            self._stroke_scale.set(width)
        finally:
            self._stroke_refreshing = False
        self._update_color_previews()
        self._draw_stroke_sample()
        targets = self.canvas._stroke_targets()
        if targets and isinstance(targets[0], ImageLayer):
            # Keep the Image tab's border fields in step with the stroke panel.
            self._img_border_var.set(str(targets[0].border_width))
            self._img_border_color_var.set(targets[0].border_color)

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
        if getattr(self, "_stroke_refreshing", False):
            return
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
            fx = layer.effects
            self._fx_shadow_var.set(fx.shadow)
            self._fx_shadow_offset_var.set(str(fx.shadow_offset))
            self._fx_shadow_blur_var.set(str(fx.shadow_blur))
            self._fx_shadow_opacity_var.set(str(round(fx.shadow_opacity * 100)))
            self._fx_shadow_color_var.set(fx.shadow_color)
            self._fx_bevel_var.set(fx.bevel)
            self._fx_bevel_size_var.set(str(fx.bevel_size))
            self._fx_emboss_var.set(fx.emboss)
            self._fx_emboss_strength_var.set(str(round(fx.emboss_strength * 100)))
        elif isinstance(layer, MaskLayer):
            self._props_notebook.select(self._mask_tab)
            self._mask_shape_var.set(layer.shape)
            self._mask_radius_var.set(float(layer.corner_radius))
            for key, var in self._mask_side_vars.items():
                var.set(key in layer.sides)
            self._mask_flip_var.set(layer.flipped)
            self._mask_peaks_var.set(str(layer.peaks))
            self._mask_amp_var.set(str(layer.amplitude))
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
        self._refresh_mask_cancel_button()
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
        """Apply the chosen sides or flip to the selected edge mask."""
        if self._updating_props:
            return
        layer = self.canvas.selected_layer()
        if not isinstance(layer, MaskLayer) or layer.shape not in EDGE_SHAPES:
            return
        sides = tuple(key for key in EDGE_SIDE_LABELS if self._mask_side_vars[key].get())
        flipped = bool(self._mask_flip_var.get())
        try:
            peaks = max(0, int(float(self._mask_peaks_var.get() or 0)))
            amplitude = max(0, int(float(self._mask_amp_var.get() or 0)))
        except ValueError:
            return
        if (layer.sides, layer.flipped, layer.peaks, layer.amplitude) == (sides, flipped, peaks, amplitude):
            return
        self.canvas.push_undo_checkpoint()
        layer.sides = sides
        layer.flipped = flipped
        layer.peaks = peaks
        layer.amplitude = amplitude
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
        flatten_source_rect(layer)
        layer.width = max(MIN_LAYER_SIZE, w)
        layer.height = max(MIN_LAYER_SIZE, h)
        record_resize_percent(layer)
        layer.opacity = clamp(opacity, 0, 100) / 100.0
        layer.border_width = max(0, border)
        layer.border_color = self._img_border_color_var.get()
        layer.stroke_after_mask = self._img_stroke_after_var.get()
        self.canvas.redraw()

    def _apply_effects_props(self) -> None:
        layer = self.canvas.selected_layer()
        if not isinstance(layer, ImageLayer):
            messagebox.showinfo("Effects", "Select an image on the board first.")
            return
        try:
            offset = int(self._fx_shadow_offset_var.get())
            blur = int(self._fx_shadow_blur_var.get())
            shadow_opacity = int(self._fx_shadow_opacity_var.get())
            bevel_size = int(self._fx_bevel_size_var.get())
            emboss_strength = int(self._fx_emboss_strength_var.get())
        except ValueError:
            messagebox.showerror("Effects", "Numeric fields must be integers.")
            return
        shadow_color = self._fx_shadow_color_var.get().strip() or "#000000"
        try:
            ImageColor.getrgb(shadow_color)
        except ValueError:
            messagebox.showerror("Effects", f"Shadow color '{shadow_color}' is not a valid color.")
            return
        self.canvas.push_undo_checkpoint()
        layer.effects = ImageEffects(
            shadow=self._fx_shadow_var.get(),
            shadow_offset=clamp(offset, 0, 200),
            shadow_blur=clamp(blur, 0, 100),
            shadow_color=shadow_color,
            shadow_opacity=clamp(shadow_opacity, 0, 100) / 100.0,
            bevel=self._fx_bevel_var.get(),
            bevel_size=clamp(bevel_size, 1, 50),
            emboss=self._fx_emboss_var.get(),
            emboss_strength=clamp(emboss_strength, 0, 100) / 100.0,
        )
        self.canvas.redraw()

    def _reset_image_size(self) -> None:
        layer = self.canvas.selected_layer()
        if not isinstance(layer, ImageLayer):
            return
        self.canvas.push_undo_checkpoint()
        flatten_source_rect(layer)
        layer.width = float(layer.source.width)
        layer.height = float(layer.source.height)
        record_resize_percent(layer)
        self._on_selection_changed(layer)

    def _page_dpi(self) -> float:
        """DPI used to convert inches to pixels. Raises ValueError when it is not a positive number."""
        try:
            dpi = float(self._page_dpi_var.get())
        except ValueError:
            raise ValueError("DPI must be a number.") from None
        if dpi <= 0:
            raise ValueError("DPI must be greater than zero.")
        return dpi

    def _read_page_entries(self) -> tuple[int, int]:
        """Return the Width and Height fields as whole pixels. Raises ValueError with a user-facing message."""
        if self._page_unit_var.get() == "in":
            dpi = self._page_dpi()
            try:
                w_in = float(self._page_w_var.get())
                h_in = float(self._page_h_var.get())
            except ValueError:
                raise ValueError("Width and height must be numbers of inches.") from None
            return round(w_in * dpi), round(h_in * dpi)
        try:
            return int(self._page_w_var.get()), int(self._page_h_var.get())
        except ValueError:
            raise ValueError("Width and height must be integers.") from None

    def _format_page_value(self, px: int) -> str:
        if self._page_unit_var.get() == "in":
            return f"{px / self._page_dpi():.2f}".rstrip("0").rstrip(".")
        return str(px)

    def _set_page_entries(self, w: int, h: int) -> None:
        self._page_w_var.set(self._format_page_value(w))
        self._page_h_var.set(self._format_page_value(h))

    def _on_page_unit_changed(self) -> None:
        """Keep the same page size when switching between pixels and inches."""
        try:
            w, h = self._read_in_units(self._page_unit_last)
        except ValueError:
            # Not a valid number in the old unit; show the page's real size instead.
            w, h = self.canvas.page_width, self.canvas.page_height
        self._page_unit_last = self._page_unit_var.get()
        unit = "in" if self._page_unit_last == "in" else "px"
        self._page_w_label.configure(text=f"Width ({unit})")
        self._page_h_label.configure(text=f"Height ({unit})")
        self._set_page_entries(w, h)

    def _read_in_units(self, unit: str) -> tuple[int, int]:
        """Read the entries as if they were in the given unit ('px' or 'in')."""
        saved = self._page_unit_var.get()
        self._page_unit_var.set(unit)
        try:
            return self._read_page_entries()
        finally:
            self._page_unit_var.set(saved)

    def _apply_page_from_panel(self) -> None:
        try:
            w, h = self._read_page_entries()
        except ValueError as exc:
            messagebox.showerror("Page", str(exc))
            return
        if self._landscape_var.get():
            # Landscape keeps the long side horizontal; a portrait preset is turned on its side.
            w, h = max(w, h), min(w, h)
            self._set_page_entries(w, h)
        bg = self._page_bg_var.get().strip() or "#ffffff"
        self.canvas.set_page_size(w, h, bg=bg)
        self.canvas.paint_layer = Image.new("RGB", (self.canvas.page_width, self.canvas.page_height), bg)
        self.canvas.redraw()
        self._refresh_page_info()

    def _set_default_page(self) -> None:
        """Save the current page size and background as the default for new boards and startup."""
        canvas = self.canvas
        update_settings(default_page={"width": canvas.page_width, "height": canvas.page_height, "bg": canvas.page_bg})
        messagebox.showinfo(
            "Default page",
            f"New boards and the next launch will start at {canvas.page_width} × {canvas.page_height} px "
            f"with background {canvas.page_bg}.",
        )

    def _refresh_preset_choices(self) -> None:
        self._preset_combo.configure(values=list(all_page_presets()))

    def _save_page_preset(self) -> None:
        """Save the Width and Height fields as a named preset in the dropdown."""
        try:
            w, h = self._read_page_entries()
        except ValueError as exc:
            messagebox.showerror("Page preset", str(exc))
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
        self._set_page_entries(w, h)
        self._apply_page_from_panel()

    def _edit_stroke_dialog(self, layer: ImageLayer) -> None:
        """Edit the border of the selected images. Changes preview live; Cancel restores them."""
        canvas = self.canvas
        images = [item for item in canvas.selected_layers() if isinstance(item, ImageLayer)] or [layer]
        originals = [(item, item.border_width, item.border_color, item.stroke_after_mask) for item in images]
        first = images[0]
        width_var = tk.StringVar(value=str(first.border_width))
        color_var = tk.StringVar(value=first.border_color)
        follow_var = tk.BooleanVar(value=first.stroke_after_mask)
        pushed = {"undo": False}

        dialog = tk.Toplevel(self)
        count = len(images)
        dialog.title(f"Edit stroke ({count} image{'s' if count != 1 else ''})")
        dialog.configure(bg=self._current_palette()["root"])
        dialog.transient(self)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Width (px):").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=0, to=COLLAGE_MAX_STROKE, textvariable=width_var, width=8).grid(
            row=0, column=1, sticky="w", padx=(8, 0), pady=4
        )
        ttk.Label(frame, text="Color:").grid(row=1, column=0, sticky="w", pady=4)
        color_row = ttk.Frame(frame)
        color_row.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Entry(color_row, textvariable=color_var, width=10).pack(side=tk.LEFT)

        def pick_color() -> None:
            chosen = colorchooser.askcolor(color=color_var.get(), title="Stroke color", parent=dialog)
            if chosen[1]:
                color_var.set(chosen[1])

        ttk.Button(color_row, text="Pick…", command=pick_color).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(
            frame,
            text="Follow the mask edge",
            variable=follow_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(
            frame,
            text="Width 0 removes the stroke.",
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def apply_live(*_args: object) -> None:
            try:
                width = int(width_var.get().strip())
            except ValueError:
                return
            color = color_var.get().strip()
            if not 0 <= width <= COLLAGE_MAX_STROKE:
                return
            if len(color) != 7 or not color.startswith("#") or any(ch not in "0123456789abcdefABCDEF" for ch in color[1:]):
                return
            follow = bool(follow_var.get())
            if all(
                item.border_width == width and item.border_color == color and item.stroke_after_mask == follow
                for item in images
            ):
                return
            if not pushed["undo"]:
                canvas.push_undo_checkpoint()
                pushed["undo"] = True
            for item in images:
                item.border_width = width
                item.border_color = color
                item.stroke_after_mask = follow
            canvas.redraw()

        width_var.trace_add("write", apply_live)
        color_var.trace_add("write", apply_live)
        follow_var.trace_add("write", apply_live)

        def remove_stroke() -> None:
            width_var.set("0")

        def cancel() -> None:
            for item, width, color, follow in originals:
                item.border_width = width
                item.border_color = color
                item.stroke_after_mask = follow
            if pushed["undo"] and canvas._undo_stack:
                # Drop the undo step made for this edit; the board is back as it was.
                canvas._undo_stack.pop()
            canvas.redraw()
            dialog.destroy()

        def done() -> None:
            canvas._on_selection_change(canvas.selected_layer())
            canvas.redraw()
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="Remove stroke", command=remove_stroke).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Done", command=done).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda _e: cancel())
        dialog.bind("<Return>", lambda _e: done())
        dialog.grab_set()

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

    def _history_marker(self) -> tuple[int, int, int]:
        canvas = self.canvas
        return (canvas.history_version, len(canvas._undo_stack), len(canvas._redo_stack))

    def _mark_saved(self) -> None:
        self._saved_marker = self._history_marker()

    def _has_unsaved_changes(self) -> bool:
        return self._saved_marker is not None and self._history_marker() != self._saved_marker

    def _set_project_path(self, path: Optional[Path]) -> None:
        self._project_path = path
        self.title(f"Manifestation Board Studio — {path.name if path else 'Untitled'}")

    def _confirm_unsaved(self) -> bool:
        """True when the board can be replaced. Offers to save changes first."""
        if not self._has_unsaved_changes():
            return True
        answer = messagebox.askyesnocancel("Unsaved changes", "Save changes to this collage first?")
        if answer is None:
            return False
        if answer:
            return self._project_save()
        return True

    def _write_project(self, path: Path) -> bool:
        canvas = self.canvas
        try:
            save_project_file(path, canvas.layers, canvas.paint_layer, canvas.page_width, canvas.page_height, canvas.page_bg)
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror("Save failed", f"Couldn't save {path.name}:\n{exc}")
            return False
        self._set_project_path(path)
        self._mark_saved()
        return True

    def _project_save(self) -> bool:
        if self._project_path is None:
            return self._project_save_as()
        return self._write_project(self._project_path)

    def _project_save_as(self) -> bool:
        path_str = filedialog.asksaveasfilename(
            title="Save collage",
            defaultextension=PROJECT_EXTENSION,
            filetypes=[("Manifestation board collage", f"*{PROJECT_EXTENSION}")],
            initialfile=self._project_path.name if self._project_path else f"collage{PROJECT_EXTENSION}",
        )
        if not path_str:
            return False
        return self._write_project(Path(path_str))

    def _project_open(self) -> None:
        if not self._confirm_unsaved():
            return
        path_str = filedialog.askopenfilename(
            title="Open collage",
            filetypes=[("Manifestation board collage", f"*{PROJECT_EXTENSION}")],
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            data = load_project_file(path)
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            messagebox.showerror("Open failed", f"Couldn't open {path.name}:\n{exc}")
            return
        canvas = self.canvas
        width = int(data["page_width"])  # type: ignore[arg-type]
        height = int(data["page_height"])  # type: ignore[arg-type]
        bg = str(data["page_bg"])
        canvas.set_page_size(width, height, bg=bg)
        canvas.paint_layer = data["paint_layer"]  # type: ignore[assignment]
        canvas.layers = list(data["layers"])  # type: ignore[arg-type]
        canvas.set_selection([], notify=False)
        canvas.clear_history()
        self._page_w_var.set(str(width))
        self._page_h_var.set(str(height))
        self._page_bg_var.set(bg)
        canvas._on_selection_change(None)
        canvas.redraw()
        self._refresh_page_info()
        self._set_project_path(path)
        self._mark_saved()

    def _refresh_mask_cancel_button(self) -> None:
        active = self.canvas.mask_session is not None
        self._cancel_mask_btn.state(["!disabled"] if active else ["disabled"])

    def _remove_selected_mask(self) -> None:
        """Delete the selected mask. The image keeps its position and pixels."""
        layer = self.canvas.selected_layer()
        if not isinstance(layer, MaskLayer):
            messagebox.showinfo("Remove mask", "Select a mask on the board first.")
            return
        if self.canvas.delete_mask(layer.id):
            self._on_selection_changed(self.canvas.selected_layer())

    def _fit_mask_to_image(self) -> None:
        """Make the selected mask exactly match its image's position and size."""
        layer = self.canvas.selected_layer()
        if not isinstance(layer, MaskLayer):
            messagebox.showinfo("Fit to image", "Select a mask on the board first.")
            return
        if self.canvas.fit_mask_to_image(layer.id):
            self._on_selection_changed(layer)

    def _cancel_mask_edit(self) -> None:
        """Exit the mask edit and undo what it changed. Applied masks and other work are kept."""
        if not self.canvas.cancel_mask_edit():
            messagebox.showinfo("Cancel mask edit", "No mask edit is in progress.")
        self._refresh_mask_cancel_button()

    def _exit_app(self) -> None:
        if self._confirm_unsaved():
            self.destroy()

    def _new_board(self) -> None:
        if not self._confirm_unsaved():
            return
        if not messagebox.askyesno("New board", "Clear the current board and start fresh?"):
            return
        self.canvas.layers.clear()
        self.canvas.select_layer(None)
        width, height, bg = load_default_page()
        self.canvas.set_page_size(width, height, bg=bg)
        self._page_w_var.set(str(width))
        self._page_h_var.set(str(height))
        self._page_bg_var.set(bg)
        self._refresh_page_info()
        self.canvas.clear_history()
        self.canvas.redraw()
        self._set_project_path(None)
        self._mark_saved()

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

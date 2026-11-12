"""Shape masks: L-mode alpha masks for crop shapes and edge cuts."""

from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw, ImageFont

from board_core.common import EDGE_DEPTH, EDGE_SHAPES, EdgeSide, MaskShape, clamp


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


# Peaks per edge when the user hasn't chosen a number.
EDGE_DEFAULT_PEAKS: dict[str, int] = {"edge_wave": 2, "edge_zigzag": 3, "edge_scallop": 3, "edge_arc": 1}


def _edge_profile(kind: str, t: float, peaks: int = 0) -> float:
    """Cut depth, 0..1, at position t (0..1) along an edge shape's side.

    peaks is the number of waves, teeth or bumps along the side; 0 uses the shape's default."""
    n = peaks if peaks > 0 else EDGE_DEFAULT_PEAKS.get(kind, 1)
    if kind == "edge_wave":
        return 0.5 - 0.5 * math.cos(2 * n * math.pi * t)
    if kind == "edge_zigzag":
        # Triangle wave: zero at both corners so the cut never reaches them.
        return 1.0 - abs(2 * ((n * t) % 1.0) - 1.0)
    if kind == "edge_scallop":
        # Rounded bumps. Sine bumps meet the corners gently, so the cut stays off them.
        return math.sin(math.pi * ((n * t) % 1.0))
    if kind == "edge_arc":
        return math.sin(math.pi * t)
    return 0.0


def _edge_mask(
    width: int,
    height: int,
    kind: str,
    side: EdgeSide,
    flipped: bool,
    peaks: int = 0,
    amplitude: int = 0,
) -> Image.Image:
    """Rectangle whose one side follows an edge profile. The profile is drawn on the bottom
    side and then rotated or mirrored onto the requested side."""
    if side in ("left", "right"):
        cw, ch = height, width
    else:
        cw, ch = width, height
    # amplitude is the peak depth in pixels; 0 uses a fraction of the box.
    depth = float(amplitude) if amplitude > 0 else EDGE_DEPTH * ch
    depth = min(depth, ch - 1.0)
    steps = max(2, cw)
    points = [(0.0, 0.0), (float(cw), 0.0)]
    for i in range(steps, -1, -1):
        t = i / steps
        f = _edge_profile(kind, t, peaks)
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


_DOLLAR_FONT_CANDIDATES = ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf")


def _dollar_font(size: int) -> ImageFont.ImageFont:
    """A bold sans font at the given size, falling back to Pillow's built-in font."""
    for name in _DOLLAR_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_dollar(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    """Draw a centred $ sign, sized so its glyph is about 90% of the box's shorter side."""
    target = 0.9 * min(width, height)
    size = max(8, int(target))
    font = _dollar_font(size)
    for _ in range(4):
        left, top, right, bottom = font.getbbox("$")
        glyph_h = bottom - top
        if glyph_h <= 0:
            break
        new_size = max(8, round(size * target / glyph_h))
        if new_size == size:
            break
        size = new_size
        font = _dollar_font(size)
    draw.text((width / 2, height / 2), "$", font=font, fill=255, anchor="mm")


def create_shape_mask(
    width: int,
    height: int,
    shape: MaskShape,
    corner_radius: int = 32,
    sides: tuple[EdgeSide, ...] = ("bottom",),
    flipped: bool = False,
    peaks: int = 0,
    amplitude: int = 0,
) -> Image.Image:
    """Build an L-mode mask (255 = visible) for the given shape."""
    width = max(1, width)
    height = max(1, height)
    if shape in EDGE_SHAPES:
        # Each selected side cuts the box; a pixel stays visible only if no cut removes it.
        result = Image.new("L", (width, height), 255)
        for side in dict.fromkeys(sides):
            result = ImageChops.multiply(result, _edge_mask(width, height, shape, side, flipped, peaks, amplitude))
        return result
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
    elif shape == "dollar":
        _draw_dollar(draw, width, height)
    elif shape == "frame":
        # A rectangular ring: the image shows only in a border about 12% of the shorter side thick.
        thickness = max(2, round(min(width, height) * 0.12))
        draw.rectangle((0, 0, width - 1, height - 1), fill=255)
        if width > 2 * thickness and height > 2 * thickness:
            draw.rectangle((thickness, thickness, width - 1 - thickness, height - 1 - thickness), fill=0)
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

"""Manifestation board editor: resize images, collage on a custom poster, draw, and export."""

from __future__ import annotations

import math
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from tkinter import colorchooser, filedialog, messagebox, ttk
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
UNDO_LIMIT = 50
CLIPBOARD_PASTE_OFFSET = 20.0

ToolName = Literal["select", "line", "rect", "ellipse", "fill", "crop"]
LayerKind = Literal["image", "line", "rect", "ellipse"]
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
]

MASK_SHAPE_LABELS: dict[MaskShape, str] = {
    "none": "None (rectangle)",
    "circle": "Circle",
    "rounded": "Rounded rectangle",
    "heart": "Heart",
    "star": "Star",
    "hexagon": "Hexagon",
    "diamond": "Diamond",
    "octagon": "Octagon",
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


def _hex_to_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Parse #RRGGBB into an RGBA tuple."""
    color = color.lstrip("#")
    if len(color) != 6:
        return 0, 0, 0, alpha
    return (
        int(color[0:2], 16),
        int(color[2:4], 16),
        int(color[4:6], 16),
        alpha,
    )


def _star_vertices(cx: float, cy: float, outer_r: float, points: int = 5) -> list[tuple[float, float]]:
    """Return vertices for a regular star centered at (cx, cy)."""
    inner_r = outer_r * 0.42
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
    """Return vertices tracing a heart that fits the given box."""
    margin = 0.06
    box_w = width * (1 - 2 * margin)
    box_h = height * (1 - 2 * margin)
    ox = width * margin
    oy = height * margin
    verts: list[tuple[float, float]] = []
    for i in range(72):
        t = 2 * math.pi * i / 72
        hx = 16 * math.sin(t) ** 3
        hy = -(
            13 * math.cos(t)
            - 5 * math.cos(2 * t)
            - 2 * math.cos(3 * t)
            - math.cos(4 * t)
        )
        nx = (hx + 16) / 32
        ny = (hy + 17) / 34
        verts.append((ox + nx * box_w, oy + ny * box_h))
    return verts


def create_shape_mask(
    width: int,
    height: int,
    shape: MaskShape,
    corner_radius: int = 32,
) -> Image.Image:
    """Build an L-mode mask (255 = visible) for the given shape."""
    width = max(1, width)
    height = max(1, height)
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
        draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=255)
    elif shape == "heart":
        draw.polygon(_heart_vertices(width, height), fill=255)
    elif shape == "star":
        outer_r = min(width, height) / 2 - inset
        draw.polygon(_star_vertices(cx, cy, outer_r), fill=255)
    elif shape == "hexagon":
        outer_r = min(width, height) / 2 - inset
        draw.polygon(_regular_polygon_vertices(cx, cy, outer_r, 6), fill=255)
    elif shape == "octagon":
        outer_r = min(width, height) / 2 - inset
        draw.polygon(_regular_polygon_vertices(cx, cy, outer_r, 8, rotation=-math.pi / 8), fill=255)
    elif shape == "diamond":
        draw.polygon(
            [(cx, y1), (x2, cy), (cx, y2), (x1, cy)],
            fill=255,
        )
    return mask


def apply_shape_mask(image: Image.Image, mask: Image.Image) -> Image.Image:
    """Multiply image alpha by a shape mask."""
    rgba = image.convert("RGBA")
    alpha = rgba.split()[3]
    rgba.putalpha(ImageChops.multiply(alpha, mask))
    return rgba


def compose_image_layer(layer: ImageLayer) -> tuple[Image.Image, int, int]:
    """Rasterize an image layer to RGBA and return paste position (ox, oy)."""
    resized = layer.source.resize(
        (max(1, int(layer.width)), max(1, int(layer.height))),
        Image.Resampling.LANCZOS,
    )
    if layer.opacity < 1.0:
        alpha = resized.split()[3]
        alpha = alpha.point(lambda p: int(p * layer.opacity))
        resized.putalpha(alpha)

    shape: MaskShape = layer.mask_shape if layer.mask_shape in MASK_SHAPE_LABELS else "none"
    inner_mask = create_shape_mask(resized.width, resized.height, shape, layer.mask_corner_radius)
    masked = apply_shape_mask(resized, inner_mask)

    bw = max(0, layer.border_width)
    if bw == 0:
        return masked, int(layer.x), int(layer.y)

    if shape == "none":
        bordered = Image.new(
            "RGBA",
            (masked.width + bw * 2, masked.height + bw * 2),
            (0, 0, 0, 0),
        )
        draw = ImageDraw.Draw(bordered)
        draw.rectangle(
            (0, 0, bordered.width - 1, bordered.height - 1),
            outline=layer.border_color,
            width=bw,
        )
        bordered.paste(masked, (bw, bw), masked)
        return bordered, int(layer.x) - bw, int(layer.y) - bw

    outer_w = masked.width + bw * 2
    outer_h = masked.height + bw * 2
    outer_mask = create_shape_mask(outer_w, outer_h, shape, layer.mask_corner_radius + bw)
    inner_on_outer = Image.new("L", (outer_w, outer_h), 0)
    inner_on_outer.paste(inner_mask, (bw, bw))
    ring = ImageChops.subtract(outer_mask, inner_on_outer)
    border_rgba = Image.new("RGBA", (outer_w, outer_h), _hex_to_rgba(layer.border_color))
    border_rgba.putalpha(ring)
    result = Image.new("RGBA", (outer_w, outer_h), (0, 0, 0, 0))
    result = Image.alpha_composite(result, border_rgba)
    result.paste(masked, (bw, bw), masked)
    return result, int(layer.x) - bw, int(layer.y) - bw


def copy_image_layer(layer: ImageLayer) -> ImageLayer:
    """Return a copy of an image layer for drag snapshots (same id, shared source)."""
    return ImageLayer(
        id=layer.id,
        source=layer.source,
        source_path=layer.source_path,
        x=layer.x,
        y=layer.y,
        width=layer.width,
        height=layer.height,
        opacity=layer.opacity,
        border_width=layer.border_width,
        border_color=layer.border_color,
        mask_shape=layer.mask_shape,
        mask_corner_radius=layer.mask_corner_radius,
    )


def clone_board_layer(layer: BoardLayer, *, new_id: bool = False) -> BoardLayer:
    """Deep-copy a board layer; optionally assign a new id."""
    layer_id = new_id() if new_id else layer.id
    if isinstance(layer, ImageLayer):
        return ImageLayer(
            id=layer_id,
            source=layer.source.copy(),
            source_path=layer.source_path,
            x=layer.x,
            y=layer.y,
            width=layer.width,
            height=layer.height,
            opacity=layer.opacity,
            border_width=layer.border_width,
            border_color=layer.border_color,
            mask_shape=layer.mask_shape,
            mask_corner_radius=layer.mask_corner_radius,
        )
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
    mask_shape: MaskShape = "none"
    mask_corner_radius: int = 32


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


BoardLayer = Union[ImageLayer, LineLayer, RectLayer, EllipseLayer]


def layer_display_name(layer: BoardLayer, stack_index: int) -> str:
    """Short label for layer pickers (1-based stack index)."""
    prefix = f"{stack_index + 1}."
    if isinstance(layer, ImageLayer):
        if layer.source_path is not None:
            return f"{prefix} {layer.source_path.name}"
        return f"{prefix} Image"
    return f"{prefix} {layer.kind.capitalize()}"


PAGE_PRESETS: dict[str, tuple[int, int]] = {
    "HD screen (1920×1080)": (1920, 1080),
    "Portrait phone (1080×1920)": (1080, 1920),
    "US Letter @ 150 DPI (1275×1650)": (1275, 1650),
    "US Letter @ 300 DPI (2550×3300)": (2550, 3300),
    "A4 @ 150 DPI (1240×1754)": (1240, 1754),
    "Poster 24×36 @ 150 DPI (3600×5400)": (3600, 5400),
}


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
        self.page_width = 1920
        self.page_height = 1080
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
        self.selected_id: Optional[str] = None
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._board_photo: Optional[ImageTk.PhotoImage] = None
        self._drag_mode: Optional[str] = None
        self._drag_start_page: Optional[tuple[float, float]] = None
        self._drag_layer_snapshot: Optional[BoardLayer] = None
        self._shape_start: Optional[tuple[float, float]] = None
        self._temp_shape_id: Optional[str] = None
        self._crop_target_id: Optional[str] = None
        self._crop_end_page: Optional[tuple[float, float]] = None
        self._undo_stack: list[BoardSnapshot] = []
        self._redo_stack: list[BoardSnapshot] = []
        self._restoring_history = False
        self._clipboard_layer: Optional[BoardLayer] = None
        self._drag_had_undo = False

        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", self._on_wheel_linux)
        self.bind("<Button-5>", self._on_wheel_linux)

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
        """Copy the selected layer to the internal clipboard."""
        layer = self.selected_layer()
        if layer is None:
            return False
        self._clipboard_layer = clone_board_layer(layer, new_id=True)
        return True

    def cut_selected(self) -> bool:
        """Cut the selected layer to the internal clipboard."""
        if not self.copy_selected():
            return False
        self.push_undo_checkpoint()
        self.delete_selected(record_undo=False)
        return True

    def paste_clipboard(self) -> bool:
        """Paste the internal clipboard as a new layer."""
        if self._clipboard_layer is None:
            return False
        self.push_undo_checkpoint()
        pasted = clone_board_layer(self._clipboard_layer, new_id=True)
        self._layer_move(pasted, CLIPBOARD_PASTE_OFFSET, CLIPBOARD_PASTE_OFFSET)
        self.add_layer(pasted, record_undo=False)
        return True

    def set_tool(self, tool: ToolName) -> None:
        """Activate a canvas tool."""
        self.tool = tool
        if tool not in ("select", "crop"):
            self.select_layer(None)

    def select_layer(self, layer_id: Optional[str], *, notify: bool = True) -> None:
        """Select a layer by id or clear selection."""
        self.selected_id = layer_id
        if notify:
            layer = self.get_layer(layer_id) if layer_id else None
            self._on_selection_change(layer)
        else:
            self.redraw()

    def get_layer(self, layer_id: Optional[str]) -> Optional[BoardLayer]:
        """Return layer by id."""
        if layer_id is None:
            return None
        for layer in self.layers:
            if layer.id == layer_id:
                return layer
        return None

    def selected_layer(self) -> Optional[BoardLayer]:
        """Return the currently selected layer."""
        return self.get_layer(self.selected_id)

    def add_layer(self, layer: BoardLayer, *, record_undo: bool = True) -> None:
        """Append a layer and select it."""
        if record_undo and not self._restoring_history:
            self.push_undo_checkpoint()
        self.layers.append(layer)
        self.select_layer(layer.id)

    def delete_selected(self, *, record_undo: bool = True) -> None:
        """Remove the selected layer."""
        if self.selected_id is None:
            return
        if record_undo and not self._restoring_history:
            self.push_undo_checkpoint()
        self.layers = [layer for layer in self.layers if layer.id != self.selected_id]
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

    def _layer_move(self, layer: BoardLayer, dx: float, dy: float) -> None:
        """Translate a layer by page-space delta."""
        if dx == 0 and dy == 0:
            return
        if isinstance(layer, ImageLayer):
            layer.x += dx
            layer.y += dy
        elif isinstance(layer, (LineLayer, RectLayer, EllipseLayer)):
            layer.x1 += dx
            layer.y1 += dy
            layer.x2 += dx
            layer.y2 += dy

    def _align_layer_to_bounds(
        self,
        layer: BoardLayer,
        alignment: AlignMode,
        bounds_x1: float,
        bounds_y1: float,
        bounds_x2: float,
        bounds_y2: float,
    ) -> None:
        """Move layer so its bbox matches alignment relative to the given bounds."""
        x1, y1, x2, y2 = self._layer_bbox(layer)
        width = x2 - x1
        height = y2 - y1
        dx = 0.0
        dy = 0.0
        if alignment == "left":
            dx = bounds_x1 - x1
        elif alignment == "right":
            dx = bounds_x2 - x2
        elif alignment == "center_h":
            ref_cx = (bounds_x1 + bounds_x2) / 2
            dx = ref_cx - width / 2 - x1
        elif alignment == "top":
            dy = bounds_y1 - y1
        elif alignment == "bottom":
            dy = bounds_y2 - y2
        elif alignment == "center_v":
            ref_cy = (bounds_y1 + bounds_y2) / 2
            dy = ref_cy - height / 2 - y1
        elif alignment == "center":
            ref_cx = (bounds_x1 + bounds_x2) / 2
            ref_cy = (bounds_y1 + bounds_y2) / 2
            dx = ref_cx - width / 2 - x1
            dy = ref_cy - height / 2 - y1
        self._layer_move(layer, dx, dy)

    def align_selected(self, alignment: AlignMode) -> None:
        """Align the selected layer to a page edge or center."""
        layer = self.selected_layer()
        if layer is None:
            return
        self.push_undo_checkpoint()
        self._align_layer_to_bounds(layer, alignment, 0.0, 0.0, self.page_width, self.page_height)
        self._on_selection_change(layer)
        self.redraw()

    def align_selected_to_layer(self, alignment: AlignMode, reference_id: str) -> None:
        """Align the selected layer to the reference layer's bounding box."""
        layer = self.selected_layer()
        reference = self.get_layer(reference_id)
        if layer is None or reference is None or layer.id == reference_id:
            return
        self.push_undo_checkpoint()
        ref_x1, ref_y1, ref_x2, ref_y2 = self._layer_bbox(reference)
        self._align_layer_to_bounds(layer, alignment, ref_x1, ref_y1, ref_x2, ref_y2)
        self._on_selection_change(layer)
        self.redraw()

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

    def render_to_image(self) -> Image.Image:
        """Composite the board at full page resolution."""
        base = self.paint_layer.convert("RGBA")

        for layer in self.layers:
            if isinstance(layer, ImageLayer):
                composited, ox, oy = compose_image_layer(layer)
                base.paste(composited, (ox, oy), composited)
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
        if isinstance(layer, ImageLayer):
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

    def _hit_handle(self, px: float, py: float, layer: ImageLayer) -> Optional[str]:
        x1, y1, x2, y2 = self._layer_bbox(layer)
        points = {
            "nw": (x1, y1),
            "ne": (x2, y1),
            "sw": (x1, y2),
            "se": (x2, y2),
        }
        tol = HANDLE_RADIUS / max(self.zoom, 1e-6)
        for name, (hx, hy) in points.items():
            if abs(px - hx) <= tol and abs(py - hy) <= tol:
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
            layer = self.selected_layer()
            if isinstance(layer, ImageLayer):
                handle = self._hit_handle(px, py, layer)
                if handle:
                    self._begin_drag_undo()
                    self._drag_mode = handle
                    self._drag_start_page = (px, py)
                    self._drag_layer_snapshot = copy_image_layer(layer)
                    return
            hit = self._hit_test(px, py)
            if hit is not None:
                self.select_layer(hit.id)
                if isinstance(hit, ImageLayer):
                    self._begin_drag_undo()
                    self._drag_mode = "move"
                    self._drag_start_page = (px, py)
                    self._drag_layer_snapshot = copy_image_layer(hit)
                return
            self.select_layer(None)
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

    def _on_motion(self, event: tk.Event) -> None:
        px, py = self.canvas_to_page(event.x, event.y)
        px = clamp(px, 0, self.page_width)
        py = clamp(py, 0, self.page_height)

        if self._drag_mode and self._drag_start_page and self._drag_layer_snapshot:
            layer = self.selected_layer()
            if not isinstance(layer, ImageLayer):
                return
            snap = self._drag_layer_snapshot
            sx, sy = self._drag_start_page
            dx, dy = px - sx, py - sy
            if self._drag_mode == "move":
                layer.x = snap.x + dx
                layer.y = snap.y + dy
            else:
                x1, y1, x2, y2 = snap.x, snap.y, snap.x + snap.width, snap.y + snap.height
                if self._drag_mode in ("se", "ne"):
                    x2 = x2 + dx
                if self._drag_mode in ("sw", "nw"):
                    x1 = x1 + dx
                if self._drag_mode in ("se", "sw"):
                    y2 = y2 + dy
                if self._drag_mode in ("ne", "nw"):
                    y1 = y1 + dy
                if x2 - x1 < MIN_LAYER_SIZE:
                    x2 = x1 + MIN_LAYER_SIZE
                if y2 - y1 < MIN_LAYER_SIZE:
                    y2 = y1 + MIN_LAYER_SIZE
                layer.x = x1
                layer.y = y1
                layer.width = x2 - x1
                layer.height = y2 - y1
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

    def _on_release(self, _event: tk.Event) -> None:
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
        self._drag_layer_snapshot = None
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

        layer = self.selected_layer()
        if isinstance(layer, ImageLayer):
            x1, y1 = self.page_to_canvas(layer.x, layer.y)
            x2, y2 = self.page_to_canvas(layer.x + layer.width, layer.y + layer.height)
            self.create_rectangle(x1, y1, x2, y2, outline="#4a9eff", width=2, dash=(4, 2))
            for hx, hy in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
                self.create_oval(
                    hx - HANDLE_RADIUS,
                    hy - HANDLE_RADIUS,
                    hx + HANDLE_RADIUS,
                    hy + HANDLE_RADIUS,
                    fill="#ffffff",
                    outline="#4a9eff",
                    width=2,
                )

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


class AssetPanel(ttk.Frame):
    """Library of images to resize before placing on the board."""

    def __init__(
        self,
        master: tk.Misc,
        on_place: Callable[[Image.Image, Optional[Path]], None],
        **kwargs: object,
    ) -> None:
        super().__init__(master, **kwargs)
        self._on_place = on_place
        self._assets: list[tuple[Path, Image.Image]] = []
        self._thumb_photos: list[ImageTk.PhotoImage] = []

        header = ttk.Frame(self)
        header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(header, text="Image assets").pack(side=tk.LEFT)
        ttk.Button(header, text="Add…", command=self._add_files).pack(side=tk.RIGHT)

        resize = ttk.LabelFrame(self, text="Resize before place", padding=6)
        resize.pack(fill=tk.X, pady=4)
        self._asset_width = tk.StringVar(value="400")
        self._asset_height = tk.StringVar(value="400")
        self._maintain_aspect = tk.BooleanVar(value=True)
        ttk.Label(resize, text="W").grid(row=0, column=0, sticky="w")
        ttk.Entry(resize, textvariable=self._asset_width, width=8).grid(row=0, column=1, padx=4)
        ttk.Label(resize, text="H").grid(row=0, column=2, sticky="w")
        ttk.Entry(resize, textvariable=self._asset_height, width=8).grid(row=0, column=3, padx=4)
        ttk.Checkbutton(
            resize,
            text="Keep aspect",
            variable=self._maintain_aspect,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(list_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox = tk.Listbox(list_frame, height=8, yscrollcommand=scroll.set)
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=self._listbox.yview)
        self._listbox.bind("<Double-Button-1>", lambda _e: self._place_selected())

        ttk.Button(self, text="Place on board", command=self._place_selected).pack(fill=tk.X, pady=4)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add images",
            filetypes=[
                ("Images", "*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp;*.tif;*.tiff"),
            ],
        )
        for path_str in paths:
            path = Path(path_str)
            if not is_image_path(path):
                continue
            try:
                with Image.open(path) as img:
                    img.load()
                    rgba = img.convert("RGBA")
            except OSError:
                continue
            self._assets.append((path, rgba))
            self._listbox.insert(tk.END, path.name)

    def _place_selected(self) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        path, source = self._assets[sel[0]]
        try:
            w = int(self._asset_width.get())
            h = int(self._asset_height.get())
        except ValueError:
            messagebox.showerror("Resize", "Width and height must be integers.")
            return
        w = max(MIN_OUTPUT_SIZE, min(MAX_OUTPUT_SIZE, w))
        h = max(MIN_OUTPUT_SIZE, min(MAX_OUTPUT_SIZE, h))
        if self._maintain_aspect.get():
            ratio = source.width / max(source.height, 1)
            h = max(MIN_OUTPUT_SIZE, int(round(w / ratio)))
        resized = source.resize((w, h), Image.Resampling.LANCZOS)
        self._on_place(resized, path)


class ManifestationBoardApp(tk.Tk):
    """Main manifestation board window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Manifestation Board Studio")
        self.minsize(1100, 720)
        self.geometry("1280x800")
        self._updating_props = False
        self._build_layout()
        self._build_menubar()
        self._install_edit_shortcuts()
        self._apply_theme()

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.configure(bg="#ececec")

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New board…", command=self._new_board)
        file_menu.add_command(label="Import images to board…", command=self._import_to_board)
        file_menu.add_separator()
        file_menu.add_command(label="Export board as image…", command=self._export_board)
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

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Zoom to fit", command=self._zoom_fit)
        view_menu.add_command(label="Zoom in", command=lambda: self._zoom_by(1.15))
        view_menu.add_command(label="Zoom out", command=lambda: self._zoom_by(1 / 1.15))

        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Page size & background…", command=self._page_settings_dialog)

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

        colors = ttk.LabelFrame(left, text="Draw colors", padding=8)
        colors.pack(fill=tk.X, pady=(0, 8))
        self._stroke_preview = tk.Label(colors, text="Stroke", width=10, relief=tk.GROOVE)
        self._stroke_preview.grid(row=0, column=0, padx=2, pady=2)
        ttk.Button(colors, text="Pick stroke…", command=self._pick_stroke).grid(row=0, column=1, padx=2)
        self._fill_preview = tk.Label(colors, text="Fill", width=10, relief=tk.GROOVE)
        self._fill_preview.grid(row=1, column=0, padx=2, pady=2)
        ttk.Button(colors, text="Pick fill…", command=self._pick_fill).grid(row=1, column=1, padx=2)
        ttk.Label(colors, text="Stroke width").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._stroke_width_var = tk.StringVar(value="3")
        stroke_spin = ttk.Spinbox(colors, from_=1, to=48, textvariable=self._stroke_width_var, width=6)
        stroke_spin.grid(row=2, column=1, sticky="w", pady=(6, 0))
        stroke_spin.bind("<FocusOut>", self._sync_stroke_width)
        stroke_spin.bind("<Return>", self._sync_stroke_width)
        ttk.Label(colors, text="Fill tolerance").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self._fill_tol_var = tk.StringVar(value="32")
        tol_spin = ttk.Spinbox(colors, from_=0, to=128, textvariable=self._fill_tol_var, width=6)
        tol_spin.grid(row=3, column=1, sticky="w", pady=(4, 0))
        tol_spin.bind("<FocusOut>", self._sync_fill_tol)
        tol_spin.bind("<Return>", self._sync_fill_tol)

        assets = AssetPanel(left, on_place=self._place_asset)
        assets.pack(fill=tk.BOTH, expand=True)

        center = ttk.Frame(body)
        body.add(center, weight=1)
        status = ttk.Frame(center)
        status.pack(fill=tk.X, pady=(0, 4))
        self._page_info_var = tk.StringVar()
        ttk.Label(status, textvariable=self._page_info_var).pack(side=tk.LEFT)
        ttk.Label(status, text="Scroll wheel zooms").pack(side=tk.RIGHT)

        self.canvas = PosterCanvas(center, on_selection_change=self._on_selection_changed)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._update_color_previews()
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
        ttk.Label(page_tab, text="Preset").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._preset_var = tk.StringVar(value="HD screen (1920×1080)")
        preset = ttk.Combobox(
            page_tab,
            textvariable=self._preset_var,
            values=list(PAGE_PRESETS.keys()),
            state="readonly",
            width=22,
        )
        preset.grid(row=2, column=1, pady=(8, 0), sticky="w")
        preset.bind("<<ComboboxSelected>>", self._apply_preset)
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
        self._img_mask_var = tk.StringVar(value=MASK_SHAPE_LABELS["none"])
        self._img_mask_radius_var = tk.StringVar(value="32")
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
        ttk.Label(image_tab, text="Shape mask").grid(row=5, column=0, sticky="w", pady=(8, 0))
        mask_combo = ttk.Combobox(
            image_tab,
            textvariable=self._img_mask_var,
            values=list(MASK_SHAPE_LABELS.values()),
            state="readonly",
            width=18,
        )
        mask_combo.grid(row=5, column=1, pady=(8, 0), sticky="w")
        ttk.Label(image_tab, text="Corner radius").grid(row=6, column=0, sticky="w")
        ttk.Entry(image_tab, textvariable=self._img_mask_radius_var, width=10).grid(
            row=6,
            column=1,
            pady=2,
        )
        ttk.Label(
            image_tab,
            text="Use corner radius for rounded masks. Border follows the shape.",
            wraplength=220,
            foreground="#555555",
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
            text="Select a line or shape, then use draw colors and stroke width on the left.",
            wraplength=220,
        ).pack(anchor="w", pady=(0, 8))
        self._build_align_controls(shape_tab, use_pack=True)

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
        selected_id = self.canvas.selected_id
        labels: list[str] = []
        mapping: dict[str, str] = {}
        for index, layer in enumerate(self.canvas.layers):
            if layer.id == selected_id:
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

    def _refresh_page_info(self) -> None:
        self._page_info_var.set(
            f"Board: {self.canvas.page_width} × {self.canvas.page_height} px  ·  "
            f"Zoom: {int(self.canvas.zoom * 100)}%",
        )

    def _on_tool_change(self) -> None:
        tool = self._tool_var.get()
        if tool in ("select", "line", "rect", "ellipse", "fill", "crop"):
            self.canvas.set_tool(tool)  # type: ignore[arg-type]

    def _update_color_previews(self) -> None:
        self._stroke_preview.configure(bg=self.canvas.stroke_color)
        self._fill_preview.configure(bg=self.canvas.fill_color)

    def _pick_stroke(self) -> None:
        color = colorchooser.askcolor(color=self.canvas.stroke_color, title="Stroke color")
        if color[1]:
            self.canvas.stroke_color = color[1]
            self._update_color_previews()

    def _pick_fill(self) -> None:
        color = colorchooser.askcolor(color=self.canvas.fill_color, title="Fill color")
        if color[1]:
            self.canvas.fill_color = color[1]
            self._update_color_previews()

    def _sync_stroke_width(self, _event: Optional[tk.Event] = None) -> None:
        try:
            self.canvas.stroke_width = int(self._stroke_width_var.get())
        except ValueError:
            pass

    def _sync_fill_tol(self, _event: Optional[tk.Event] = None) -> None:
        try:
            self.canvas.fill_tolerance = int(self._fill_tol_var.get())
        except ValueError:
            pass

    def _place_asset(self, image: Image.Image, path: Optional[Path]) -> None:
        layer = ImageLayer(
            source=image,
            source_path=path,
            x=(self.canvas.page_width - image.width) / 2,
            y=(self.canvas.page_height - image.height) / 2,
            width=float(image.width),
            height=float(image.height),
        )
        self.canvas.add_layer(layer)

    def _on_selection_changed(self, layer: Optional[BoardLayer]) -> None:
        self._updating_props = True
        if isinstance(layer, ImageLayer):
            self._props_notebook.select(1)
            self._img_w_var.set(str(int(layer.width)))
            self._img_h_var.set(str(int(layer.height)))
            self._img_opacity_var.set(str(int(layer.opacity * 100)))
            self._img_border_var.set(str(layer.border_width))
            self._img_border_color_var.set(layer.border_color)
            self._img_mask_var.set(MASK_SHAPE_LABELS.get(layer.mask_shape, MASK_SHAPE_LABELS["none"]))
            self._img_mask_radius_var.set(str(layer.mask_corner_radius))
        self._updating_props = False
        self._refresh_align_reference_choices()
        self.canvas.redraw()
        self._refresh_page_info()

    @staticmethod
    def _mask_shape_from_label(label: str) -> MaskShape:
        """Map UI label to mask shape key."""
        for key, text in MASK_SHAPE_LABELS.items():
            if text == label:
                return key
        return "none"

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
            corner_radius = int(self._img_mask_radius_var.get())
        except ValueError:
            messagebox.showerror("Image", "Numeric fields must be integers.")
            return
        self.canvas.push_undo_checkpoint()
        layer.width = max(MIN_LAYER_SIZE, w)
        layer.height = max(MIN_LAYER_SIZE, h)
        layer.opacity = clamp(opacity, 0, 100) / 100.0
        layer.border_width = max(0, border)
        layer.border_color = self._img_border_color_var.get()
        layer.mask_shape = self._mask_shape_from_label(self._img_mask_var.get())
        layer.mask_corner_radius = max(0, corner_radius)
        self.canvas.redraw()

    def _reset_image_size(self) -> None:
        layer = self.canvas.selected_layer()
        if not isinstance(layer, ImageLayer):
            return
        self.canvas.push_undo_checkpoint()
        layer.width = float(layer.source.width)
        layer.height = float(layer.source.height)
        self._on_selection_changed(layer)

    def _apply_page_from_panel(self) -> None:
        try:
            w = int(self._page_w_var.get())
            h = int(self._page_h_var.get())
        except ValueError:
            messagebox.showerror("Page", "Width and height must be integers.")
            return
        bg = self._page_bg_var.get().strip() or "#ffffff"
        self.canvas.set_page_size(w, h, bg=bg)
        self.canvas.paint_layer = Image.new("RGB", (self.canvas.page_width, self.canvas.page_height), bg)
        self.canvas.redraw()
        self._refresh_page_info()

    def _apply_preset(self, _event: Optional[tk.Event] = None) -> None:
        preset = PAGE_PRESETS.get(self._preset_var.get())
        if preset is None:
            return
        w, h = preset
        self._page_w_var.set(str(w))
        self._page_h_var.set(str(h))
        self._apply_page_from_panel()

    def _page_settings_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Page settings")
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

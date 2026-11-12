# Manifestation Board

A desktop editor for building manifestation boards and photo collages. Import images, arrange or collage them on a custom-sized page, crop them into shapes, draw on top, and export the result as an image or a printable PDF.

Built with Python and Tkinter. The only third-party dependency is [Pillow](https://python-pillow.org/).

## Features

- **Pages:** choose from preset sizes or set a custom size and background. Save a default page for new boards.
- **Images:** import PNG, JPEG, GIF, BMP, WEBP and TIFF files. Move and resize them, and align them to the page or to other layers.
- **Shape masks:** crop an image to a circle, heart, star, hexagon, diamond, octagon, triangle, pentagon, cross, arrow, dollar sign or border frame. Four edge styles (wave, zigzag, scallop, arc) cut one or more sides.
- **Arrange and collage:** *Arrange images* lays images out in an even grid, weighted by their importance presets. *Make collage* packs them to cover the page with little overlap.
- **Drawing:** lines, rectangles and ellipses, with stroke and fill styles. Crop tool and undo/redo history, with a timeline you can jump through.
- **AI edits:** right-click an image and choose *Generate with AI…* to edit it with OpenAI's image model (default `gpt-image-1`). Requires an API key.
- **Export:** save the board as PNG, JPEG or WEBP, or export print tiles as a multi-page PDF at a chosen paper size and PPI.
- **Collage files:** save and reopen work as `.mbd` files, which bundle the layers and their images in a single zip.
- **Look:** light and dark UI themes. Document content (the page and its layers) is never themed.

## Requirements

- Python (developed and tested on 3.14; older versions are untested)
- Pillow 10 or newer
- Tkinter, which ships with the standard Windows and macOS Python installers. On Linux it may need a separate package such as `python3-tk`.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

To run the tests as well, install the development requirements instead:

```bash
pip install -r requirements-dev.txt
```

## Running

```bash
python image_resizer.py
```

## OpenAI key (optional)

The AI edit feature reads your key from, in order:

1. The `OPENAI_API_KEY` environment variable
2. A `.env` file next to `image_resizer.py`, containing `OPENAI_API_KEY=sk-...`
3. The key typed into the AI panel

The AI panel has a *Remember* option. If you tick it, the key is saved in plain text to `~/.image_resizer_settings.json`. Leave it off on shared machines. `.env` is git-ignored, so keep your key there rather than in source files.

## Where things are stored

| What | Where |
|---|---|
| UI and default settings (page, dark mode, resize presets, custom page sizes, last-used options) | `~/.image_resizer_settings.json` |
| Your board | Wherever you save it, as a `.mbd` file |

## Running the tests

```bash
python -m pytest
```

Tests live in `tests/` and cover the pure logic that the editor uses: shape masks, shelf and grid layouts, collage tiling and corner-drag constraints. They do not open a window.

Two tests are marked as *expected failures* (`xfail`) because they document known issues:

- The *scallop* edge cut removes the bottom-right corner pixel of an image by a fraction of a pixel. The edge profile is meant to leave corners solid.
- A 1-pixel-tall circle mask raises `ValueError`. I haven't found a path in the app that creates a mask that small, so this is low priority.

## Project layout

```
image_resizer.py        Tk application: window, canvas, panels, dialogs, export and file I/O
board_core/             Pure logic with no Tk dependency, so it can be tested directly
    common.py           Shared type aliases (MaskShape, EdgeSide) and clamp()
    masks.py            Shape and edge-cut mask generation
    layout.py           Shelf and grid layouts, collage tiling, corner-drag constraint
tests/                  pytest suite for board_core
pytest.ini              Test configuration
requirements.txt        Runtime dependencies
requirements-dev.txt    Runtime plus test dependencies
```

New layout and mask behaviour should go in `board_core/`, with a test in `tests/`. The UI in `image_resizer.py` should stay thin and call into it.

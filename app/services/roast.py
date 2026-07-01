"""
"Wall of Shame" meme generator.

Fetches classic meme images from api.memegen.link. All captions only joke about
the act of forgetting to sign out — never about a person.

Custom memes: add {"custom_img": "filename.jpg", "lines": ["...", "..."]}
to TEMPLATES. Drop the image file in app/services/custom_memes/.

For text at arbitrary spots (not just top/bottom), drop a sidecar layout file
next to the image (<image_stem>.yml) defining positioned overlay rectangles.
The template's "lines" fill those overlays in order. See _load_overlays.
"""
import io
import random
from pathlib import Path
from typing import Optional

import aiohttp
import yaml
from PIL import Image, ImageDraw, ImageFont


_CUSTOM_MEMES_DIR = Path(__file__).parent / "custom_memes"

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

"""
MEMEGEN API TEMPLATES
{"id": "drake", "lines": ["{names} signing out", "{names} getting made fun of in #memes"]}

CUSTOM MEME TEMPLATES
{"id": "mentor-bob", "custom_img": "bob.jpg", "lines": ["{names} forgot to sign out", "Bob showing them for the 4th time"]}

CUSTOM MEME WITH POSITIONED TEXT (layout in <image_stem>.yml; "lines" fill overlays)
{"id": "uno", "custom_img": "uno.jpg", "lines": ["{names}, sign out"]}
  ...alongside uno.yml holding: overlays: [{anchor_x, anchor_y, scale_x, scale_y, ...}]
"""

TEMPLATES: list[dict] = [
    {"id": "drake",         "lines": ["{names} signing out", "{names} getting made fun of in #memes"]},
    {"id": "fry",           "lines": ["Not sure if {names} is still working", "or they forgot to sign out"]},
    {"id": "gears",         "lines": ["you know what really grinds my gears?", "{names} not signing out when they leave"]},
    {"id": "officespace",   "lines": ["Yeah...", "{names}, I am going to need you to sign out"]},
    {"id": "wddth",         "lines": ["{names} when asked about how to sign out", "We dont do that here"]},
    {"id": "wishes",        "lines": ["{names} wants to leave and not sign out"]},
    {"id": "gru",           "lines": ["{names} shows up to robotics", "works a full session", "leaves without signing out", "leaves without signing out"]},
    {"id": "headaches",     "lines": ["{names} not signing out"]},
    {"id": "afraid",        "lines": ["{names} doesnt know how to sign out", "and at this point they are too afraid to ask"]},
    {"id": "db",            "lines": ["walking out", "{names}", "signing out"]},
    {"id": "exit",          "lines": ["signing out", "walking out", "{names}"]},
    {"id": "right",         "lines": ["{names}", "Mercury Bot", "I just completed a full session at robotics", "You signed out when you left, right?", "You signed out when you left, right?"]},
    {"id": "say",           "lines": ["Say the line {names}!", "I forgot to sign out.."]},
    {"id": "mordor",        "lines": ["{names} does not simply", "sign out when they leave"]},
    # Custom Meme Templates
    {"id": "uno",           "custom_img": "uno/uno.jpg", "lines": ["sign out", "{names}"]},
]


def _build_lines(t: dict, name: str) -> list[str]:
    if "lines" in t:
        lines = t["lines"]
    elif t.get("bottom") is None:
        lines = [t["top"]]
    else:
        lines = [t["top"], t["bottom"]]
    return [line.format(names=name) for line in lines]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _render_custom_meme(img_path: Path, lines: list[str]) -> bytes:
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    font_size = max(24, int(h * 0.08))
    font = _load_font(font_size)

    padding = int(h * 0.03)
    stroke = max(2, font_size // 12)

    def draw_text_line(text: str, y: int) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        x = (w - text_w) // 2
        draw.text((x, y), text, font=font, fill="white", stroke_width=stroke, stroke_fill="black")

    if len(lines) == 1:
        draw_text_line(lines[0], padding)
    elif len(lines) == 2:
        # top line at top, bottom line at bottom
        bottom_bbox = draw.textbbox((0, 0), lines[1], font=font)
        bottom_h = bottom_bbox[3] - bottom_bbox[1]
        draw_text_line(lines[0], padding)
        draw_text_line(lines[1], h - bottom_h - padding)
    else:
        # evenly space N lines from top to bottom
        line_h = draw.textbbox((0, 0), "A", font=font)[3]
        step = (h - 2 * padding) // (len(lines) - 1)
        for i, line in enumerate(lines):
            draw_text_line(line, padding + i * step - line_h // 2)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """Word-wrap text (honoring explicit newlines) so each line fits max_w pixels."""
    out: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            trial = f"{cur} {word}"
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                out.append(cur)
                cur = word
        out.append(cur)
    return out


_overlay_cache: dict[str, list[dict]] = {}


def _load_overlays(img_path: Path) -> Optional[list[dict]]:
    """Load an image's sidecar layout config (<image_stem>.yml), or None if absent.

    Mirrors memegen's config.yml: a list of text overlays, each a rectangle placed
    by fractions of the image size. The Nth caption line fills the Nth overlay.

      overlays:
        - anchor_x: 0.05     # top-left corner (fraction of width)
          anchor_y: 0.22     # top-left corner (fraction of height)
          scale_x:  0.34     # rectangle width  (fraction of width)
          scale_y:  0.16     # rectangle height (fraction of height)
          angle:    0        # optional rotation, degrees counter-clockwise
          align:    center   # left | center | right
          color:    black    # text fill (default white)
          stroke_fill: white # outline color (default black)
    """
    cfg = img_path.with_suffix(".yml")
    if not cfg.exists():
        return None
    key = str(cfg)
    if key not in _overlay_cache:
        data = yaml.safe_load(cfg.read_text()) or {}
        _overlay_cache[key] = data.get("overlays") or []
    return _overlay_cache[key]


def _fit_and_wrap(draw: ImageDraw.ImageDraw, text: str, rect_w: int, rect_h: int):
    """Find the largest font size whose word-wrapped text fits the rectangle."""
    for size in range(rect_h, 7, -1):
        font = _load_font(size)
        lines = _wrap_text(draw, text, font, rect_w)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if line_h * len(lines) <= rect_h and widest <= rect_w:
            return font, lines, line_h, size
    font = _load_font(8)
    ascent, descent = font.getmetrics()
    return font, _wrap_text(draw, text, font, rect_w), ascent + descent, 8


def _draw_overlay(base: Image.Image, overlay: dict, text: str) -> None:
    """Render one caption into its overlay rectangle, with optional rotation."""
    w, h = base.size
    rw = max(1, int(overlay.get("scale_x", 1.0) * w))
    rh = max(1, int(overlay.get("scale_y", 0.2) * h))
    rx = int(overlay.get("anchor_x", 0.0) * w)
    ry = int(overlay.get("anchor_y", 0.0) * h)

    # Draw onto a transparent tile so we can rotate the whole block if asked.
    tile = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    font, lines, line_h, size = _fit_and_wrap(draw, text, rw, rh)

    stroke = max(1, size // 12)
    align = overlay.get("align", "center")
    color = overlay.get("color", "white")
    stroke_fill = overlay.get("stroke_fill", "black")
    top = (rh - line_h * len(lines)) // 2  # vertically center the block in the rect

    for i, line in enumerate(lines):
        line_w = draw.textlength(line, font=font)
        if align == "left":
            x = 0.0
        elif align == "right":
            x = rw - line_w
        else:
            x = (rw - line_w) / 2
        draw.text(
            (x, top + i * line_h), line, font=font, fill=color,
            stroke_width=stroke, stroke_fill=stroke_fill,
        )

    angle = overlay.get("angle", 0)
    if angle:
        tile = tile.rotate(angle, expand=True, resample=Image.BICUBIC)

    # Paste centered on the rectangle's center (matters when rotation grew the tile).
    cx, cy = rx + rw // 2, ry + rh // 2
    base.paste(tile, (cx - tile.width // 2, cy - tile.height // 2), tile)


def _render_config_meme(img_path: Path, overlays: list[dict], lines: list[str]) -> bytes:
    img = Image.open(img_path).convert("RGBA")
    for overlay, line in zip(overlays, lines):
        _draw_overlay(img, overlay, line)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _memegen_encode(text: str) -> str:
    return (
        text
        .replace("_", "__")
        .replace(" ", "_")
        .replace("?", "~q")
        .replace("'", "''")
        .replace("/", "~s")
        .replace("#", "~h")
    )


async def fetch_meme(names: list[str], template: Optional[dict] = None) -> bytes:
    """Return a meme PNG for the given names. Raises on failure."""
    t = template or random.choice(TEMPLATES)
    name = names[0] if names else "Someone"

    if "custom_img" in t:
        img_path = _CUSTOM_MEMES_DIR / t["custom_img"]
        lines = _build_lines(t, name)
        overlays = _load_overlays(img_path)
        if overlays:
            return _render_config_meme(img_path, overlays, lines)
        return _render_custom_meme(img_path, lines)

    lines = _build_lines(t, name)
    encoded = "/".join(_memegen_encode(line) for line in lines)
    url = f"https://api.memegen.link/images/{t['id']}/{encoded}.png"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.read()

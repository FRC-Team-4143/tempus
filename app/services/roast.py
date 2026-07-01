"""
"Wall of Shame" meme generator.

Fetches classic meme images from api.memegen.link. All captions only joke about
the act of forgetting to sign out — never about a person.

Custom memes: add {"custom_img": "filename.jpg", "lines": ["...", "..."]}
to TEMPLATES. Drop the image file in app/services/custom_memes/.
"""
import io
import random
from pathlib import Path
from typing import Optional

import aiohttp
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
"""

TEMPLATES: list[dict] = [
    {"id": "drake",         "lines": ["{names} signing out",                    "{names} getting made fun of in #memes"]},
    {"id": "fry",           "lines": ["Not sure if {names} is still working",    "or they forgot to sign out"]},
    {"id": "gears",         "lines": ["you know what really grinds my gears?",   "{names} not signing out when they leave"]},
    {"id": "officespace",   "lines": ["Yeah...",                                 "{names}, I am going to need you to sign out"]},
    {"id": "wddth",         "lines": ["{names} when asked about how to sign out", "We dont do that here"]},
    {"id": "wishes",        "lines": ["{names} wants to leave and not sign out"]},
    {"id": "gru",           "lines": ["{names} shows up to robotics", "works a full session", "leaves without signing out", "leaves without signing out"]},
    {"id": "headaches",     "lines": ["{names} not signing out"]},
    {"id": "afraid",        "lines": ["{names} doesnt know how to sign out", "and at this point they are too afriad to ask"]},
    {"id": "db",            "lines": ["walking out", "{names}", "signing out"]},
    {"id": "exit",          "lines": ["signing out", "walking out", "{names}"]},
    {"id": "right",         "lines": ["{names}", "Mercury Bot", "I just completed a full session at robotics", "You signed out when you left, right?", "You signed out when you left, right?"]},
    {"id": "say",          "lines": ["Say the line {names}!", "I forgot to sign out.."]},
    {"id": "mordor",          "lines": ["{names} does not simply", "sign out when they leave"]},
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
    lines = _build_lines(t, name)

    if "custom_img" in t:
        img_path = _CUSTOM_MEMES_DIR / t["custom_img"]
        return _render_custom_meme(img_path, lines)

    encoded = "/".join(_memegen_encode(line) for line in lines)
    url = f"https://api.memegen.link/images/{t['id']}/{encoded}.png"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.read()

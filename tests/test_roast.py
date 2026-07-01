"""Tests for the Wall of Shame meme generator."""
import io

from PIL import Image

from app.services.roast import TEMPLATES, _render_custom_meme


def test_templates_structure():
    assert TEMPLATES, "template pool must not be empty"
    for t in TEMPLATES:
        assert t.get("id"), f"template missing id: {t!r}"
        if "lines" in t:
            assert len(t["lines"]) >= 1, f"lines must not be empty: {t!r}"
            has_names = any("{names}" in line for line in t["lines"])
        else:
            assert t.get("top") is not None, f"template missing top: {t!r}"
            has_names = "{names}" in t["top"] or "{names}" in (t.get("bottom") or "")
        assert has_names, f"no line contains {{names}}: {t!r}"


def test_custom_meme_render(tmp_path):
    # Create a small solid-colour image as a stand-in for a real photo.
    img = Image.new("RGB", (400, 300), color=(30, 80, 160))
    img_path = tmp_path / "test.jpg"
    img.save(img_path)

    result = _render_custom_meme(img_path, ["Alex forgot to sign out", "classic Alex"])

    assert isinstance(result, bytes)
    assert len(result) > 0
    out = Image.open(io.BytesIO(result))
    assert out.format == "PNG"
    assert out.size == (400, 300)

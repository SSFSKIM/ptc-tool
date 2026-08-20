"""Unit test for the per-image cap in ptc.mcp._content (T8 review finding: images
were base64-encoded before any per-file size check existed, so one oversized
screenshot could dominate the whole 4MB aggregate content budget on its own).
Keyless: no PTC_HOME, no kernel — render() and _content() are pure functions over
a fabricated outcome, same fixture-free style as test/unit/test_shape.py.
"""
import base64

from ptc.cells import CellRecord
from ptc.client import Completed
from ptc.mcp import _content
from ptc.paths import Config
from ptc.shape import render


def _rec(**kw):
    base = dict(status="ok", duration_ms=10, result_repr=None, error=None, images=[], mutations=[])
    base.update(kw)
    return CellRecord(**base)


def test_per_image_cap_skips_oversized_image_keeps_small_one(tmp_path):
    small = tmp_path / "small.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1_000)   # well under the 1.5MB cap

    big = tmp_path / "big.png"
    big.write_bytes(b"y" * 1_500_001)                         # one byte over the cap

    outcome = Completed(1, _rec(images=[str(small), str(big)]), "output\n")
    rendered = render(outcome, "k", Config.from_env(env={}))
    assert rendered.images == [small, big]                    # both exist, both survive render()

    out = _content(rendered)

    assert len(out) == 3                                      # header text + 1 image + 1 skip note
    header, image_item, skip_item = out

    assert header.type == "text" and header.text == rendered.text

    assert image_item.type == "image"
    assert image_item.mime_type == "image/png"
    assert base64.b64decode(image_item.data) == small.read_bytes()

    assert skip_item.type == "text"
    assert "big.png" in skip_item.text
    assert "skipped" in skip_item.text
    assert "1500001 bytes" in skip_item.text
    assert "1.5MB per-image cap" in skip_item.text
    assert str(big) in skip_item.text

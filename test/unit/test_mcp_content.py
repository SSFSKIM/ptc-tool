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


# --- r14 finding 6: an image moved mid-reply does not cost the reply ------------------

def test_an_image_moved_after_render_costs_a_note_not_the_whole_response(tmp_path):
    """`render()` verifies these paths and `_content` then stats and reads them — and a
    concurrent restart rotating `cells/` into `cells-prev-*` fits between the two. The
    unguarded reads raised FileNotFoundError out of the handler, so the caller lost the
    cell's TEXT (the thing it asked for) and every image that was still there, over one
    file that had merely moved.

    The note names the convention rather than the vanished file, because a reader who
    wants that image has to know where a restart puts it.
    """
    cells = tmp_path / "cells"
    cells.mkdir()
    gone, kept = cells / "1-0.png", cells / "1-1.png"
    for p in (gone, kept):
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)

    outcome = Completed(1, _rec(images=[str(gone), str(kept)]), "the output\n")
    rendered = render(outcome, "k", Config.from_env(env={}))
    assert rendered.images == [gone, kept], "render() must have verified both"

    gone.unlink()                                  # the restart moved it out from under us

    out = _content(rendered)

    assert out[0].type == "text" and out[0].text == rendered.text, "the text must survive"
    assert out[1].type == "text"
    assert "1-0.png" in out[1].text and "cells-prev-*" in out[1].text
    assert out[2].type == "image", "a later image was dropped with the missing one"
    assert base64.b64decode(out[2].data) == kept.read_bytes()


def test_a_whole_rotated_cells_directory_still_returns_the_text(tmp_path):
    """The real shape of the race: not one file, the whole directory renamed at once."""
    cells = tmp_path / "cells"
    cells.mkdir()
    img = cells / "2-0.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)

    rendered = render(Completed(2, _rec(images=[str(img)]), "still here\n"), "k",
                      Config.from_env(env={}))
    cells.rename(tmp_path / "cells-prev-9")

    out = _content(rendered)

    assert len(out) == 2
    assert out[0].text == rendered.text
    assert "2-0.png" in out[1].text and "cells-prev-*" in out[1].text

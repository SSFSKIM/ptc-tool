"""How a cell log is sliced into bounded reads (`cells.read_since`).

Pure function, pure filesystem. A caller resumes at the offset it was handed, so the whole
contract is that consecutive slices concatenate back into what was written — which is
exactly what a per-chunk `errors="replace"` decode cannot promise at the seam.
"""
from ptc.cells import read_since


def _read_all(path, max_bytes: int) -> str:
    """What every caller does: resume at the offset it was handed, until a read is empty.

    Doubles as the termination assertion — a chunker that backs off as far as it advanced
    hands back an offset it has already been given and never reaches the end of the log.
    """
    text, offset = "", 0
    for _ in range(200):
        chunk, offset = read_since(path, offset, max_bytes)
        if not chunk:
            return text
        text += chunk
    raise AssertionError(f"read_since never reached the end of {path}")


def test_a_character_straddling_the_read_boundary_survives_it(tmp_path):
    """Each chunk was decoded on its own, so a multibyte character split by the boundary
    became replacement glyphs in BOTH halves — permanently, and for nothing: every byte is
    still on disk and the caller resumes at exactly the offset it was handed. The
    truncated trailing sequence is handed back to the next read instead."""
    p = tmp_path / "1.log"
    p.write_bytes("aaébb".encode())            # 'é' is two bytes, at offsets 2..3

    first, off = read_since(p, 0, 3)           # the boundary falls inside 'é'
    second, _ = read_since(p, off, 3)

    assert "�" not in first + second, f"the boundary character was destroyed: {first + second!r}"
    assert first + second == "aaébb"[:len(first + second)]
    assert _read_all(p, 3) == "aaébb"


def test_every_boundary_inside_a_multibyte_character_round_trips(tmp_path):
    """Every split position of every sequence length: the read backs off by one, two or
    three bytes and re-consumes the character whole. From four bytes up — the longest
    UTF-8 sequence — a read always advances, so the walk terminates as well as reassembles.
    """
    original = "aé😀b€c" * 3
    p = tmp_path / "2.log"
    p.write_bytes(original.encode())

    for max_bytes in range(4, len(original.encode()) + 2):
        assert _read_all(p, max_bytes) == original, f"max_bytes={max_bytes}"


def test_a_torn_tail_at_end_of_file_is_still_shown(tmp_path):
    """A SHORT read is at EOF, where a truncated character is the writer's in-progress
    state rather than an artefact of our chunking. Holding it back would hide output that
    may never be completed — `errors="replace"` remains the honest rendering of it."""
    p = tmp_path / "3.log"
    p.write_bytes(b"ok" + "é".encode()[:1])

    text, off = read_since(p, 0, 4000)

    assert text == "ok�" and off == 3


def test_a_log_of_invalid_bytes_still_decodes_and_terminates(tmp_path):
    """The back-off looks for a truncated LEAD sequence. Bytes no lead byte can explain are
    not a character anyone is waiting for: they decode with replacements as they always
    did, and above all they do not stall the reader on a boundary that can never resolve."""
    p = tmp_path / "4.log"
    p.write_bytes(b"ok\xff\xfe\xf8\xf8\xf8\xf8done")

    text = _read_all(p, 4)

    assert text.startswith("ok") and text.endswith("done") and "�" in text

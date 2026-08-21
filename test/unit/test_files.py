import pytest

from ptc.runtime import files
from ptc.runtime.state import STATE


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.current_cell = 7
    STATE.cell_mutations = []
    yield


def test_read_offset_limit_numbered(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\nd\n")
    assert files.read(p) == "a\nb\nc\nd\n"
    assert files.read(p, offset=2, limit=2) == "b\nc\n"
    assert files.read(p, offset=2, limit=1, numbered=True) == "     2\tb\n"


def test_write_creates_parents_and_audits(tmp_path):
    out = files.write(tmp_path / "new" / "dir" / "x.md", "one\ntwo\n")
    assert out.startswith("Wrote") and "(2 lines)" in out
    assert STATE.cell_mutations[-1]["kind"] == "write"


def test_edit_exactly_once_rules(tmp_path):
    p = tmp_path / "s.py"
    p.write_text("aaa\nbbb\naaa\n")
    with pytest.raises(ValueError, match="string not found"):
        files.edit(p, "zzz", "y")
    with pytest.raises(ValueError, match="found 2 occurrences .* widen the snippet"):
        files.edit(p, "aaa", "yyy")
    msg = files.edit(p, "bbb", "BBB\nBB2")
    assert msg.startswith("Edited") and "(+2/−1)" in msg
    assert p.read_text() == "aaa\nBBB\nBB2\naaa\n"
    msg2 = files.edit(p, "aaa", "A", replace_all=True)
    assert p.read_text() == "A\nBBB\nBB2\nA\n"
    m = STATE.cell_mutations[-1]
    assert m["kind"] == "edit" and m["path"].endswith("s.py")

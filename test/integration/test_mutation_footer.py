import asyncio

from ptc.kernel import kill_kernel
from ptc.mcp import exec_tool


def test_edit_footer_and_audit(ptc_home, tmp_path):
    f = tmp_path / "t.py"
    f.write_text("def a():\n    return 1\n")
    code = f"edit({str(f)!r}, 'return 1', 'return 2')"
    r = asyncio.run(exec_tool(code=code, session="f1", timeout_s=60))
    assert "edited" in r[0].text and "(+1/−1)" in r[0].text
    audit = (ptc_home / "kernels" / "f1" / "audit.jsonl").read_text()
    assert "t.py" in audit
    kill_kernel("f1")


def test_kernel_edit_shows_a_diff_block(ptc_home):
    """End-to-end: an in-kernel edit's diff reaches the rendered cell result."""
    from ptc.client import Completed, KernelClient
    from ptc.kernel import ensure_kernel, kill_kernel
    from ptc.paths import Config
    from ptc.shape import render
    cfg = Config.from_env()
    ensure_kernel("diffs", cwd=str(ptc_home), config=cfg)
    kc = KernelClient("diffs")
    target = ptc_home / "sample.txt"
    kc.exec_cell(f"write({str(target)!r}, 'aaa\\nbbb\\n')", timeout_s=60, config=cfg)
    out = kc.exec_cell(f"edit({str(target)!r}, 'bbb', 'ccc')", timeout_s=60, config=cfg)
    assert isinstance(out, Completed)
    text = render(out, "diffs", cfg).text
    assert "diff:" in text and "-bbb" in text and "+ccc" in text
    kill_kernel("diffs")

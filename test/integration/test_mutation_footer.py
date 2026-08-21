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

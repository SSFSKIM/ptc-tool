"""Keyless proof that `workflow` — the last runtime tool — is really bound and working
inside a live kernel, including `phase` writing to the real audit.jsonl.

The unit tests exercise the helpers directly; this one exists because binding is where
the previous runtime tool broke (T23: `__all__` named an export that import machinery had
quietly replaced with its own submodule). No credentials are involved: every awaitable
here is a plain coroutine.
"""
import json
import textwrap

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config, kernel_dir


def test_workflow_is_bound_and_runs_in_the_kernel(ptc_home):
    ensure_kernel("wf1", cwd=str(ptc_home))
    out = KernelClient("wf1").exec_cell(textwrap.dedent("""
        async def job(i):
            await asyncio.sleep(0.01)
            if i == 2:
                raise ValueError("boom")
            return i

        workflow.phase("fan out")
        res = await workflow.parallel(*(job(i) for i in range(4)), limit=2)
        print("PARALLEL", [type(r).__name__ if isinstance(r, Exception) else r for r in res])
        print("PIPELINE", await workflow.pipeline(["a", "b"], str.upper))
    """), timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed), out
    assert "── phase: fan out" in out.output, out.output
    assert "PARALLEL [0, 1, 'ValueError', 3]" in out.output, out.output
    assert "PIPELINE ['A', 'B']" in out.output, out.output

    # `phase` audits for the record. This test only proves the record was written — it
    # never renders a cell, so it cannot show what the footer does with it. That the
    # footer ignores kind `phase` is pinned by
    # test/unit/test_wf.py::test_phase_writes_an_audit_entry_the_footer_ignores.
    audit = kernel_dir("wf1") / "audit.jsonl"
    kinds = [json.loads(ln)["kind"] for ln in audit.read_text().splitlines() if ln.strip()]
    assert "phase" in kinds, kinds
    kill_kernel("wf1")

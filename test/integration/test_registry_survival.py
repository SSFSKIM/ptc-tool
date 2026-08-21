"""Registry survives kernel restart: agents.json lives under the kernel dir, not the
kernel process, and restart_kernel's _clean_stale only unlinks owner/ready/connection
plus rotates cells/ — it never touches agents.json. A fresh kernel epoch reading the
same file back is what makes agent.list() meaningful across a restart (A4's contract).
"""
import json

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel, restart_kernel
from ptc.paths import Config


def test_registry_survives_restart(ptc_home):
    ensure_kernel("rs1", cwd=str(ptc_home))
    reg = ptc_home / "kernels" / "rs1" / "agents.json"
    reg.write_text(json.dumps([{"name": "old-worker", "provider": "claude",
                                "session_id": "sess-1", "status": "done",
                                "created_at": 1.0, "last_turn_at": 2.0}]))
    restart_kernel("rs1")
    out = KernelClient("rs1").exec_cell(
        "print([e['name'] for e in agent.list()])", timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed) and "old-worker" in out.output
    kill_kernel("rs1")

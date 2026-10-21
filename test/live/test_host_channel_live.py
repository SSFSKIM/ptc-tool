"""The real host mod's result POST, against a kernel side that refuses it. Zero model calls:
a job of no known kind is answered by the mod itself, and `connect()` sends no prompt.

`host_fakes.FakeMod` stands in for the mod everywhere else; this is the one place its
bounded result retry (`RESULT_TRIES` within `RESULT_WINDOW_MS`, `host.tsx`) runs for real.

    PTC_LIVE=1 uv run pytest test/live/test_host_channel_live.py -q -rs
"""
import asyncio
import os
import time
from pathlib import Path

import pytest

from ptc.runtime import host as host_mod
from ptc.runtime.agents import AgentFailed
from ptc.runtime.host import AgentHost

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")

PLUGIN = Path(__file__).resolve().parents[2] / "host-plugin"
NO_SUCH_KIND = {"kind": "no-such-kind"}


def refusing(monkeypatch, refusals: int | None) -> list:
    """`_on_result` answering 500 to the first `refusals` POSTs (None: every one)."""
    real = AgentHost._on_result
    seen = []

    def on_result(self, gen, jid, body):
        seen.append(time.monotonic())
        if refusals is None or len(seen) <= refusals:
            return 500, {"ok": False}
        return real(self, gen, jid, body)
    monkeypatch.setattr(AgentHost, "_on_result", on_result)
    return seen


@live
def test_a_refused_result_is_posted_again_and_lands(tmp_path, monkeypatch):
    seen = refusing(monkeypatch, 1)

    async def main():
        h = AgentHost(kernel_dir=tmp_path, cwd=str(tmp_path), plugin_dir=PLUGIN)
        try:
            await h.ensure_started()
            t = time.monotonic()
            with pytest.raises(AgentFailed, match="no job kind no-such-kind"):
                await h.submit(NO_SUCH_KIND, time.monotonic() + 30)
            return time.monotonic() - t
        finally:
            await h.close()
    took = asyncio.run(main())
    assert len(seen) == 2, seen
    assert 1.5 < seen[1] - seen[0] < 4, "the retry waits RESULT_GAP_MS (2 s), not more"
    assert took < 10


@live
def test_a_result_no_post_lands_stops_the_mod_and_the_lease_retires_it(tmp_path,
                                                                        monkeypatch):
    seen = refusing(monkeypatch, None)
    monkeypatch.setattr(host_mod, "POLL_HOLD_S", 1.0)   # a held poll must not pass the lease

    async def main():
        h = AgentHost(kernel_dir=tmp_path, cwd=str(tmp_path), plugin_dir=PLUGIN, lease_s=4.0)
        try:
            await h.ensure_started()
            t = time.monotonic()
            with pytest.raises(AgentFailed, match="lease"):
                await h.submit(NO_SUCH_KIND, time.monotonic() + 40)
            return time.monotonic() - t
        finally:
            await h.close()
    took = asyncio.run(main())
    assert len(seen) == 3, "RESULT_TRIES posts, then the mod stops"
    assert seen[-1] - seen[0] < 10, "all within RESULT_WINDOW_MS"
    assert took < 25

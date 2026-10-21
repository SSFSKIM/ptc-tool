"""The SDK reader's `Fatal error in message reader` line on a real host: none for a host
the kernel is closing, one for a host that dies on its own. Zero model calls.

    PTC_LIVE=1 uv run pytest test/live/test_host_reader_live.py -q -rs
"""
import asyncio
import logging
import os
import signal
from pathlib import Path

import pytest

from ptc.runtime import host as host_mod
from ptc.runtime.host import AgentHost

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")

PLUGIN = Path(__file__).resolve().parents[2] / "host-plugin"


def reader_lines(tmp_path, closing: bool) -> int:
    """The SDK reader's `Fatal error` records when the host dies under its reader: as a
    host the kernel is closing (the race a SIGTERM'd host can win against the disconnect
    that cancels its reader, forced here), or as one that died on its own. Counted on the
    SDK's logger, where a kernel with no handler of its own prints them to the cell."""
    seen: list[str] = []

    class Count(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("Fatal error in message reader"):
                seen.append(record.getMessage())
    logger = logging.getLogger(host_mod._SDK_READER_LOGGER)
    handler = Count()
    logger.addHandler(handler)

    async def main():
        h = AgentHost(kernel_dir=tmp_path, cwd=str(tmp_path), plugin_dir=PLUGIN)
        try:
            await h.ensure_started()
            if closing:
                h._gen.proc._mark.closing = True
            os.kill(h.pid, signal.SIGKILL)
            await asyncio.sleep(2)                # the reader sees the exit first
        finally:
            await h.close()
    try:
        asyncio.run(main())
    finally:
        logger.removeHandler(handler)
    return len(seen)


@live
def test_a_host_the_kernel_is_closing_prints_no_reader_error(tmp_path):
    assert reader_lines(tmp_path, True) == 0


@live
def test_a_host_that_dies_on_its_own_says_so_once(tmp_path):
    assert reader_lines(tmp_path, False) == 1

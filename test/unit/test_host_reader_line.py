"""The SDK reader's `Fatal error in message reader` line, for a host the kernel retires
on purpose and for one that dies on its own (`host._QuietRetirement`). The real reader is
pinned live in `test/live/test_host_reader_live.py`; this pins the filter's rule.
"""
import asyncio
import logging

from ptc.runtime import host as host_mod


def test_the_readers_fatal_line_is_dropped_once_the_host_is_being_closed():
    logger = logging.getLogger(host_mod._SDK_READER_LOGGER)
    if host_mod._QUIET not in logger.filters:
        logger.addFilter(host_mod._QUIET)
    seen: list[str] = []

    class _Keep(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Keep()
    logger.addHandler(handler)
    try:
        async def reader(mark):
            # The SDK creates its reader task inside `connect()`, so the task carries the
            # context the launch set: its host's mark.
            async def log():
                logger.error("Fatal error in message reader: Command failed with exit code -15")
            token = host_mod._READER.set(mark)
            try:
                task = asyncio.get_running_loop().create_task(log())
            finally:
                host_mod._READER.reset(token)
            await task

        unexpected = host_mod._Closing()
        asyncio.run(reader(unexpected))
        assert len(seen) == 1                     # a host that died on its own says so, once

        closing = host_mod._Closing()
        closing.closing = True
        asyncio.run(reader(closing))
        assert len(seen) == 1                     # one the kernel closed says nothing

        logger.error("Fatal error in message reader: a CLI-route child")
        assert len(seen) == 2                     # nothing outside a host's reader is touched
    finally:
        logger.removeHandler(handler)

"""The `workflow` helpers: a per-fan-out bound, per-item error capture, and phase markers.

Three properties carry the design and each has a test that fails if it regresses:
the bound is real (peak concurrency, not just a parameter), `pipeline` has no
inter-stage barrier (a fast item finishes stage 2 before a slow item finishes stage 1),
and the error capture is `Exception`-only so an interrupted cell still unwinds.
"""
import asyncio
import json
import warnings

import pytest

from ptc.runtime import wf
from ptc.runtime.state import STATE


# ------------------------------------------------------------------ parallel

def test_parallel_bounded_order_and_errors(tmp_path):
    STATE.kernel_dir = tmp_path
    peak = {"now": 0, "max": 0}

    async def job(i):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.02)
        peak["now"] -= 1
        if i == 3:
            raise ValueError("boom")
        return i * 10

    out = asyncio.run(wf.parallel(*(job(i) for i in range(6)), limit=2))
    assert peak["max"] <= 2
    assert out[0] == 0 and out[5] == 50
    assert isinstance(out[3], ValueError)


def test_parallel_keeps_input_order_regardless_of_completion_order(tmp_path):
    """The bound reorders *completion*; the returned list must still line up with the
    awaitables as passed, or a caller cannot zip results back to their inputs."""
    STATE.kernel_dir = tmp_path

    async def job(i, delay):
        await asyncio.sleep(delay)
        return i

    # Completion order is 2, 1, 0; the result list must still be 0, 1, 2.
    out = asyncio.run(wf.parallel(job(0, 0.03), job(1, 0.02), job(2, 0.0), limit=8))
    assert out == [0, 1, 2]


def test_parallel_with_no_awaitables_is_an_empty_list(tmp_path):
    STATE.kernel_dir = tmp_path
    assert asyncio.run(wf.parallel()) == []


def test_parallel_rejects_a_limit_below_one(tmp_path):
    """`asyncio.Semaphore(0)` never yields a permit, so an unguarded `limit=0` would
    hang the cell forever — the one failure mode that costs the user an interrupt."""
    STATE.kernel_dir = tmp_path

    async def job():
        return 1

    coro = job()
    with pytest.raises(ValueError):
        asyncio.run(wf.parallel(coro, limit=0))
    coro.close()


def test_parallel_does_not_swallow_cancellation(tmp_path):
    """The capture is `except Exception`, deliberately: `CancelledError` is a
    `BaseException` and must keep unwinding, otherwise interrupting a fan-out would
    look like it succeeded and the kernel would appear wedged."""
    STATE.kernel_dir = tmp_path

    async def slow():
        await asyncio.sleep(10)
        return "never"

    async def main():
        task = asyncio.ensure_future(wf.parallel(slow(), slow(), limit=1))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_cancelled_fanout_leaves_no_unawaited_coroutines(tmp_path):
    """A cancelled fan-out must close the awaitables that never got a permit. Without
    that, interrupting `parallel` over a long list sprays one "coroutine ... was never
    awaited" RuntimeWarning per queued item into the cell's own output."""
    STATE.kernel_dir = tmp_path

    async def job(i):
        await asyncio.sleep(10)
        return i

    async def main():
        task = asyncio.ensure_future(wf.parallel(*(job(i) for i in range(20)), limit=1))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(main())
    never_awaited = [w for w in caught if "never awaited" in str(w.message)]
    assert not never_awaited, [str(w.message) for w in never_awaited]


# ------------------------------------------------------------------ pipeline

def test_pipeline_no_barrier(tmp_path):
    STATE.kernel_dir = tmp_path
    order = []

    async def s1(x):
        await asyncio.sleep(0.05 if x == "slow" else 0)
        order.append(("s1", x))
        return x

    async def s2(x):
        order.append(("s2", x))
        return f"{x}!"

    out = asyncio.run(wf.pipeline(["slow", "fast"], s1, s2))
    assert out == ["slow!", "fast!"]
    assert order.index(("s2", "fast")) < order.index(("s1", "slow"))   # fast finished both stages first


def test_pipeline_stage_error_drops_only_that_item(tmp_path):
    """A stage exception becomes that item's result and stops that item's chain — the
    other items run every stage."""
    STATE.kernel_dir = tmp_path
    reached_s2 = []

    async def s1(x):
        if x == "bad":
            raise RuntimeError("stage 1 failed")
        return x

    async def s2(x):
        reached_s2.append(x)
        return x.upper()

    out = asyncio.run(wf.pipeline(["a", "bad", "b"], s1, s2))
    assert out[0] == "A" and out[2] == "B"
    assert isinstance(out[1], RuntimeError)
    assert reached_s2 == ["a", "b"] or sorted(reached_s2) == ["a", "b"]


def test_pipeline_is_bounded_too(tmp_path):
    """Per-item fan-out over a caller's list is the same hazard `parallel` is bounded
    against, and stages like `bash` never touch the SDK semaphore that bounds agents."""
    STATE.kernel_dir = tmp_path
    peak = {"now": 0, "max": 0}

    async def stage(x):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.01)
        peak["now"] -= 1
        return x

    out = asyncio.run(wf.pipeline(list(range(10)), stage, limit=3))
    assert out == list(range(10))
    assert peak["max"] <= 3


def test_pipeline_accepts_sync_stages_alongside_async_ones(tmp_path):
    """A plain function is a legitimate stage. Requiring `await` on every stage would
    make each sync one raise `TypeError: object str can't be used in 'await'
    expression` — captured, so it would poison every item silently."""
    STATE.kernel_dir = tmp_path

    async def fetch(x):
        return f" {x} "

    out = asyncio.run(wf.pipeline(["a", "b"], fetch, str.strip, str.upper))
    assert out == ["A", "B"]


def test_pipeline_with_no_stages_returns_the_items(tmp_path):
    STATE.kernel_dir = tmp_path
    assert asyncio.run(wf.pipeline(["a", "b"])) == ["a", "b"]


def test_pipeline_accepts_a_generator_of_items(tmp_path):
    STATE.kernel_dir = tmp_path

    async def stage(x):
        return x * 2

    assert asyncio.run(wf.pipeline((i for i in range(4)), stage)) == [0, 2, 4, 6]


# --------------------------------------------------------------------- phase

def test_phase_prints_and_audits(tmp_path, capsys):
    STATE.kernel_dir = tmp_path
    STATE.current_cell = 1
    STATE.cell_mutations = []
    wf.phase("collect")
    assert "phase: collect" in capsys.readouterr().out


def test_phase_writes_an_audit_entry_the_footer_ignores(tmp_path, capsys):
    """`phase` is a progress marker, not a mutation: it lands in audit.jsonl for the
    record but `shape.footer_line` renders only write/edit/bash/agent, so a phase-heavy
    cell does not push real mutations out of the footer. Phases are visible in stdout."""
    from ptc.shape import footer_line

    STATE.kernel_dir = tmp_path
    STATE.current_cell = 7
    STATE.cell_mutations = []
    wf.phase("synthesize")
    capsys.readouterr()

    entries = [json.loads(ln) for ln in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert entries[-1]["kind"] == "phase"
    assert entries[-1]["name"] == "synthesize"
    assert entries[-1]["cell"] == 7
    assert STATE.cell_mutations[-1]["kind"] == "phase"
    assert footer_line(STATE.cell_mutations) is None


# ----------------------------------------------------------------- namespace

def test_workflow_namespace_exposes_the_three_helpers():
    """`workflow` is what the kernel binds; `wf` is the module. The namespace object
    must carry exactly the spec's three helpers as the same callables the module has."""
    assert wf.workflow.parallel is wf.parallel
    assert wf.workflow.pipeline is wf.pipeline
    assert wf.workflow.phase is wf.phase

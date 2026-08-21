"""Thin orchestration helpers — PTC itself is the workflow language.

These are deliberately small. A Workflow *script* in another harness is just a Python
cell here, so the helpers add only the three things a plain `asyncio.gather` cannot:
a bound on the fan-out, per-item error capture so one failure does not void the batch,
and a printed+audited progress marker.

**On the bound, and why it is not a second concurrency pool.** `agents.shared_semaphore`
bounds everything that spawns a CLI subprocess — `agent.*`, `llm()`, `web_search()` —
globally, and these helpers neither reach for it nor duplicate it. `limit` here bounds
*arbitrary* awaitables, most of which (file reads, `bash`, pure coroutines, third-party
clients) that semaphore never covered. When the awaitables happen to be SDK calls both
bounds apply and the tighter one governs; acquisition is always this permit first, then
the shared one, never the reverse, so there is no order in which two callers can deadlock
against each other.

**Fork is never a loop body** (spec S2): `agent.fork` pays a cold cache write for the
inherited parent transcript, so fanning forks out across a list is expensive by
construction. These helpers fan out whatever they are handed and cannot enforce that —
it is doctrine for the caller, carried in SKILL.md.
"""
import asyncio
import inspect

from . import audit


def _discard(aw) -> None:
    """Close a coroutine that will never be awaited. Only reachable when a fan-out is
    cancelled with items still queued for a permit: without this, interrupting
    `parallel` over a long list sprays one "coroutine ... was never awaited"
    RuntimeWarning per queued item into the cell's own output."""
    if inspect.iscoroutine(aw):
        aw.close()


def _bound(limit: int) -> asyncio.Semaphore:
    # Semaphore(0) never yields a permit and Semaphore(-1) raises — so a bad `limit`
    # would either hang the cell forever or fail obscurely. Both become one clear error.
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    return asyncio.Semaphore(limit)


async def _guarded(sem: asyncio.Semaphore, aw):
    """One permit around one awaitable, with the awaitable's own failure captured as a
    value. `except Exception` is exact: `CancelledError` is a `BaseException` and must
    keep unwinding, or interrupting a fan-out would look like it succeeded."""
    try:
        await sem.acquire()
    except BaseException:
        _discard(aw)          # never started — close it rather than leak a warning
        raise
    try:
        return await aw
    except Exception as e:                     # noqa: BLE001 — captured for the caller
        return e
    finally:
        sem.release()


async def parallel(*aws, limit: int = 8) -> list:
    """Await every awaitable with at most `limit` in flight, returning one result per
    input **in input order** — a failure is returned in place as the exception object,
    so results still zip back to their inputs."""
    sem = _bound(limit)
    return list(await asyncio.gather(*(_guarded(sem, a) for a in aws)))


async def pipeline(items, *stages, limit: int = 8) -> list:
    """Run each item through every stage, at most `limit` items in flight.

    There is no inter-stage barrier: a fast item reaches the last stage while a slow one
    is still in the first. A stage exception ends that item's chain and becomes its
    result; the other items run every stage.

    `limit` is keyword-only (it has to be, after `*stages`) and bounds the per-item
    fan-out for the same reason `parallel` does — a stage like `bash` or `web_fetch`
    never touches the SDK semaphore, so a thousand-item list would otherwise start a
    thousand of them at once.

    Stages may be sync or async. Half the natural stages in a real pipeline (a parse, a
    filter, a regex) are plain functions, and an await-only contract would turn each one
    into an opaque `TypeError` captured as that item's result — a silent poisoning of
    every item rather than an error the caller can see.
    """
    sem = _bound(limit)

    async def chain(item):
        v = item
        for s in stages:
            try:
                v = s(v)
                if inspect.isawaitable(v):
                    v = await v
            except Exception as e:             # noqa: BLE001 — captured for the caller
                return e
        return v

    # `chain` is a coroutine we construct ourselves, so it is always safe to hand to
    # _guarded; the inner try/except above is what captures stage failures, and
    # _guarded's own capture is then dead for this path by construction.
    return list(await asyncio.gather(*(_guarded(sem, chain(i)) for i in items)))


def phase(name: str) -> None:
    """A progress marker: printed for the reader, audited for the record. `phase` is not
    a mutation, and `shape.footer_line` renders only write/edit/bash/agent — so a
    phase-heavy cell cannot crowd real mutations out of the footer."""
    print(f"── phase: {name}")
    audit.append("phase", name=name)


class _Workflow:
    """The namespace object bound as `workflow` in the kernel. The module keeps the name
    `wf` so the public callable-bearing name does not collide with its own submodule."""
    parallel = staticmethod(parallel)
    pipeline = staticmethod(pipeline)
    phase = staticmethod(phase)


workflow = _Workflow()

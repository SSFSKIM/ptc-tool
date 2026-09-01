"""Render kernel outcomes into the model-facing text (shared by MCP and CLI)."""
from dataclasses import dataclass
from pathlib import Path

from .cells import CellRecord
from .client import Busy, Completed, NotFound, Running
from .paths import Config, cells_dir


@dataclass
class Rendered:
    text: str
    images: list


def _truncate(text: str, cap: int, full_log: Path) -> str:
    if len(text) <= cap:
        return text
    # cap<=0 (a misconfigured PTC_MAX_OUTPUT_CHARS) must still truncate maximally —
    # text[-0:] is the WHOLE string in Python, not empty, so an unguarded tail slice
    # would leak everything back out exactly when the strictest cap was requested.
    head_n = max(int(cap * 0.6), 0)
    tail_n = max(int(cap * 0.4), 0)
    head = text[:head_n]
    tail = text[-tail_n:] if tail_n else ""
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n… [truncated {cut} chars — full output: {full_log}]\n{tail}"


_SEP = " · "


def _more(n: int) -> str:
    return f"… and {n} more mutation" + ("" if n == 1 else "s")


def trailing_budget(cap: int) -> int:
    """The slice of the response budget EACH line trailing the body may claim.

    None of them is a free tail. A cell that writes ten thousand files produced ~179k
    characters of mutation footer appended AFTER the output was truncated, and
    `result_repr` is 4096 characters straight from the kernel — so the cap the caller asked
    for was bypassed by the lines nobody bounded. A tenth of the cap (floor 200) is enough
    for what a normal cell produces — a handful of mutations, a short repr, a one-line
    exception, all rendering byte for byte as before — and everything past it is cut.
    """
    return max(cap // 10, 200)


def _clip(line: str, budget: int) -> str:
    return line if len(line) <= budget else line[:max(budget - 1, 0)] + "…"


def _runs(mutations: list) -> list[tuple[dict, int]]:
    """Consecutive identical bash commands are one fact — a retry loop is a count, not a
    stack of entries. Only adjacent repeats collapse: A, B, A stays three."""
    out: list[tuple[dict, int]] = []
    for m in mutations:
        if (out and m.get("kind") == "bash" and out[-1][0].get("kind") == "bash"
                and out[-1][0].get("command") == m.get("command")):
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((m, 1))
    return out


def footer_line(mutations: list, budget: int | None = None) -> str | None:
    """The mutation footer, optionally bounded to `budget` characters.

    With no budget (the CLI's `--json` twin, tests, callers with their own bound) the line
    is the full join. With one, whole entries are kept while they fit and the rest becomes
    "… and N more mutations" — never a partial entry, so what is shown is always true.
    """
    parts = []
    for m, n in _runs(mutations):
        k = m.get("kind")
        if k == "edit":
            parts.append(f"edited {m.get('path')} (+{m.get('added', 0)}/−{m.get('removed', 0)})")
        elif k == "write":
            parts.append(f"wrote {m.get('path')}")
        elif k == "bash":
            # A 48-char fingerprint: the footer says what the cell DID; audit.jsonl beside
            # the kernel holds the faithful command for a reader verifying exactly what ran.
            parts.append(f"ran: {_clip(m.get('command', ''), 48)}"
                         + (f" (×{n})" if n > 1 else ""))
        elif k == "agent":
            parts.append(f"spawned agent \"{m.get('name', '?')}\"")
    if not parts:
        return None
    line = _SEP.join(parts)
    if budget is None or len(line) <= budget:
        return line
    kept: list[str] = []
    used = 0
    for i, p in enumerate(parts):
        add = len(p) + (len(_SEP) if kept else 0)
        if used + add + len(_SEP) + len(_more(len(parts) - i - 1)) > budget:
            break
        kept.append(p)
        used += add
    # the whole line did not fit, so at least one entry is always summarized here
    return _SEP.join([*kept, _more(len(parts) - len(kept))]) if kept else _more(len(parts))


def _header(cell_id, status: str, dur_ms: int | None, degraded: bool) -> str:
    dur = f" · {dur_ms / 1000:.1f}s" if dur_ms is not None else ""
    deg = " · [keying: adapter-local]" if degraded else ""
    return f"[cell {cell_id} · {status}{dur}{deg}]"


#: Busy tri-state (client.py): "running" always carries a real id; "pending-unconfirmed"
#: may carry the wedged sentinel -1; "lock-held" may carry no id at all (None). Keyed by
#: reason, used only once a real id is known — see _BUSY_TEXT_NO_ID for the id-less case.
_BUSY_TEXT = {
    "running": "cell {id} is still running",
    "pending-unconfirmed": "cell {id} was just submitted and is awaiting the kernel's confirmation",
    "lock-held": "cell {id} is busy — another submission currently holds the kernel's admission lock",
    "queue-timeout": ("cell {id} is still running — this call queued for its whole budget "
                      "and the slot never freed"),
}
_BUSY_TEXT_NO_ID = {
    # No id was ever confirmed, so no terminal record can discharge the marker either
    # (client.py `_pending_discharged`): restart() is the escape hatch, and saying so is
    # the difference between a busy kernel and one that looks permanently stuck.
    "pending-unconfirmed": ("a cell was just submitted and the kernel never confirmed its "
                            "id — nothing else is admitted until it settles or restart() "
                            "clears it"),
    "lock-held": "another submission currently holds the kernel's admission lock",
    "queue-timeout": "the slot never freed within this call's budget",
}


def render(outcome, key: str, config: Config, degraded: bool = False) -> Rendered:
    log_path = cells_dir(key)
    if isinstance(outcome, Busy):
        has_id = outcome.cell_id not in (None, -1)
        if has_id:
            which = _BUSY_TEXT.get(outcome.reason, "cell {id} is still running").format(id=outcome.cell_id)
        else:
            which = _BUSY_TEXT_NO_ID.get(outcome.reason, "a just-submitted cell is being admitted")
        # The named cell is whatever is holding a SHARED kernel — often another caller's,
        # never this one's submission (nothing was queued). Pointing wait() at that cell as
        # a source of output invited the reader to adopt another caller's result as theirs.
        #
        # A queue-timeout caller already waited for the slot, so it is told what its wait
        # cost and NOT told to pass the flag it just passed.
        queued = (f"queued {outcome.queued_s:.0f}s — "
                  if outcome.queued_s is not None else "")
        return Rendered(
            f"[kernel busy{' · [keying: adapter-local]' if degraded else ''}] "
            f"{queued}{which}. "
            + (f"That cell may belong to another caller of this shared kernel: its output is "
               f"that cell's, not the result of what you just tried to run — "
               f"wait(cell_id={outcome.cell_id}) tells you when the kernel frees, and "
               "collects that output only for the cell's own submitter. " if has_id else "")
            + ("interrupt() to stop it, or resubmit after it finishes. Nothing was queued."
               if outcome.reason == "queue-timeout" else
               "interrupt() to stop it, resubmit after it finishes, or pass queue=True "
               "to wait for the slot. Nothing was queued."), [])
    if isinstance(outcome, NotFound):
        # Said plainly, because the alternative is a caller waiting on an id that will
        # never resolve: nothing is running under this number and nothing will be.
        return Rendered(
            f"[cell {outcome.cell_id} · not found{' · [keying: adapter-local]' if degraded else ''}] "
            f"kernel {key} has no cell {outcome.cell_id} — no output, no record and no "
            "archive of one. Check the id (it may belong to a different session), or "
            "submit the code again; nothing is running under it.", [])
    if isinstance(outcome, Running):
        body = _truncate(outcome.output, config.max_output_chars, log_path / f"{outcome.cell_id}.log")
        # A matched Running came back EARLY, on the caller's `until=` pattern rather than
        # on its budget — a different event, and the reader cannot act on it without being
        # told which one it was and what fired.
        if outcome.matched is not None:
            lines = [_header(outcome.cell_id, 'running · matched', None, degraded), body]
            # The scan's window is bounded, so on a chatty cell it begins AFTER the
            # caller's cursor. Said plainly, because the guidance line below recommends
            # `since=next_offset` — which resumes past the window and can never reach the
            # dropped span. `is not None` and not truthiness: byte 0 is a real offset.
            if outcome.window_start is not None:
                lines.append(
                    f"[output before byte {outcome.window_start} is not shown: it was "
                    "read past to keep the match window bounded. It is still in the log — "
                    f"re-read it with wait(cell_id={outcome.cell_id}, "
                    f"since=<your earlier cursor>), or open "
                    f"{log_path / f'{outcome.cell_id}.log'}]")
            lines.append(
                f"[the until= pattern matched {outcome.matched!r} — the cell is STILL "
                f"RUNNING: call wait(cell_id={outcome.cell_id}, since={outcome.next_offset}) "
                "for more output, or interrupt() to stop]")
            return Rendered("\n".join(lines), [])
        return Rendered(
            f"{_header(outcome.cell_id, 'running', None, degraded)}\n{body}\n"
            f"[still running — call wait(cell_id={outcome.cell_id}, since={outcome.next_offset}) "
            "for more output, or interrupt() to stop]", [])
    rec: CellRecord = outcome.record
    # An archived cell was read out of `cells-prev-<ts>/`, and naming the live path here
    # pointed the reader of a truncated body at a file that no longer exists.
    full_log = outcome.log_path or log_path / f"{outcome.cell_id}.log"
    cap = config.max_output_chars
    lines = [_header(outcome.cell_id, rec.status, rec.duration_ms, degraded)]
    # Everything that trails the body is rendered FIRST and inside the same budget: what
    # the footer, the error summary and the result line take, the body no longer has.
    # Rendered last and unbounded, any of the three walks straight past max_output_chars —
    # the footer did it with ~179k characters of mutations, `result_repr` can do it with
    # the 4096 the kernel allows it against a cap of 100.
    f = footer_line(rec.mutations, budget=trailing_budget(cap))
    err_line = None
    if rec.status == "error" and rec.error and rec.error.get("ename") not in (None, ""):
        if rec.error["ename"] not in outcome.output:
            err_line = _clip(f"{rec.error['ename']}: {rec.error.get('evalue', '')}",
                             trailing_budget(cap))
    res_line = (_clip(f"→ result: {rec.result_repr}", trailing_budget(cap))
                if rec.result_repr is not None else None)
    if rec.result_repr is not None and rec.result_repr.startswith("<coroutine object "):
        # `bash(...)` without `await` settles `ok` with exactly this repr and nothing else
        # to distinguish it from a successful quiet cell — the silent form of the trap.
        res_line += "\n[an unawaited coroutine: nothing ran — re-run the call with await]"
    trailing = sum(len(x) for x in (f, err_line, res_line) if x)
    body = _truncate(outcome.output, cap - trailing, full_log)
    if body:
        lines.append(body.rstrip("\n"))
    lines.extend(x for x in (err_line, res_line, f) if x)
    return Rendered("\n".join(lines), [Path(p) for p in rec.images if Path(p).exists()])


def to_dict(outcome, key: str) -> dict:
    if isinstance(outcome, Busy):
        return {"status": "busy", "cell_id": outcome.cell_id, "reason": outcome.reason,
                # what a queue=True call spent waiting before it gave up (None otherwise):
                # the --json caller must see what the text render says
                "queued_s": outcome.queued_s}
    if isinstance(outcome, NotFound):
        return {"status": "not_found", "cell_id": outcome.cell_id}
    if isinstance(outcome, Running):
        return {"status": "running", "cell_id": outcome.cell_id,
                "output": outcome.output, "next_offset": outcome.next_offset,
                # what an `until=` pattern matched, None when the wait ended on its budget
                "matched": outcome.matched,
                # where `output` begins when the bounded match window dropped output the
                # scan had read past; None when it covers everything from the cursor
                "window_start": outcome.window_start}
    r = outcome.record
    return {"status": r.status, "cell_id": outcome.cell_id, "duration_ms": r.duration_ms,
            "output": outcome.output, "result_repr": r.result_repr, "error": r.error,
            "images": r.images, "mutations": r.mutations,
            # the archive, for a cell settled from a previous epoch — the live path names
            # a file that was moved away with the epoch that wrote it
            "full_log": str(outcome.log_path or cells_dir(key) / f"{outcome.cell_id}.log")}

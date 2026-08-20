"""Render kernel outcomes into the model-facing text (shared by MCP and CLI)."""
from dataclasses import dataclass
from pathlib import Path

from .cells import CellRecord
from .client import Busy, Completed, Running
from .paths import Config, cells_dir


@dataclass
class Rendered:
    text: str
    images: list


def _truncate(text: str, cap: int, full_log: Path) -> str:
    if len(text) <= cap:
        return text
    head = text[: int(cap * 0.6)]
    tail = text[-int(cap * 0.4):]
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n… [truncated {cut} chars — full output: {full_log}]\n{tail}"


def footer_line(mutations: list) -> str | None:
    parts = []
    for m in mutations:
        k = m.get("kind")
        if k == "edit":
            parts.append(f"edited {m.get('path')} (+{m.get('added', 0)}/−{m.get('removed', 0)})")
        elif k == "write":
            parts.append(f"wrote {m.get('path')}")
        elif k == "bash":
            parts.append(f"ran: {m.get('command', '')[:80]}")
        elif k == "agent":
            parts.append(f"spawned agent \"{m.get('name', '?')}\"")
    return " · ".join(parts) if parts else None


def _header(cell_id, status: str, dur_ms: int | None, degraded: bool) -> str:
    dur = f" · {dur_ms / 1000:.1f}s" if dur_ms is not None else ""
    deg = " · [keying: adapter-local]" if degraded else ""
    return f"[cell {cell_id} · {status}{dur}{deg}]"


def render(outcome, key: str, config: Config, degraded: bool = False) -> Rendered:
    log_path = cells_dir(key)
    if isinstance(outcome, Busy):
        which = ("a just-submitted cell is being admitted" if (outcome.cell_id in (None, -1))
                 else f"cell {outcome.cell_id} is still running")
        return Rendered(
            f"[kernel busy{' · [keying: adapter-local]' if degraded else ''}] "
            f"{which}. "
            + (f"Use wait(cell_id={outcome.cell_id}) for its output, " if outcome.cell_id not in (None, -1) else "")
            + "interrupt() to stop it, or resubmit after it finishes. Nothing was queued.", [])
    if isinstance(outcome, Running):
        body = _truncate(outcome.output, config.max_output_chars, log_path / f"{outcome.cell_id}.log")
        return Rendered(
            f"{_header(outcome.cell_id, 'running', None, degraded)}\n{body}\n"
            f"[still running — call wait(cell_id={outcome.cell_id}, since={outcome.next_offset}) "
            "for more output, or interrupt() to stop]", [])
    rec: CellRecord = outcome.record
    lines = [_header(outcome.cell_id, rec.status, rec.duration_ms, degraded)]
    body = _truncate(outcome.output, config.max_output_chars, log_path / f"{outcome.cell_id}.log")
    if body:
        lines.append(body.rstrip("\n"))
    if rec.status == "error" and rec.error and rec.error.get("ename") not in (None, ""):
        if rec.error["ename"] not in outcome.output:
            lines.append(f"{rec.error['ename']}: {rec.error.get('evalue', '')}")
    if rec.result_repr is not None:
        lines.append(f"→ result: {rec.result_repr}")
    f = footer_line(rec.mutations)
    if f:
        lines.append(f)
    return Rendered("\n".join(lines), [Path(p) for p in rec.images if Path(p).exists()])


def to_dict(outcome, key: str) -> dict:
    if isinstance(outcome, Busy):
        return {"status": "busy", "cell_id": outcome.cell_id}
    if isinstance(outcome, Running):
        return {"status": "running", "cell_id": outcome.cell_id,
                "output": outcome.output, "next_offset": outcome.next_offset}
    r = outcome.record
    return {"status": r.status, "cell_id": outcome.cell_id, "duration_ms": r.duration_ms,
            "output": outcome.output, "result_repr": r.result_repr, "error": r.error,
            "images": r.images, "mutations": r.mutations,
            "full_log": str(cells_dir(key) / f"{outcome.cell_id}.log")}

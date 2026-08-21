"""audit.jsonl — one line per mutation, attributed to the current cell."""
import json
import time

from ptc.paths import private_open

from .state import STATE


def append(kind: str, **fields) -> None:
    entry = {"ts": time.time(), "cell": STATE.current_cell, "kind": kind, **fields}
    STATE.cell_mutations.append(entry)
    with private_open(STATE.kernel_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def entries_for_cell(kernel_dir, cell_id: int) -> list[dict]:
    out = []
    try:
        with open(kernel_dir / "audit.jsonl") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("cell") == cell_id:
                    out.append(e)
    except OSError:
        pass
    return out

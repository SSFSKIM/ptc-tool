"""Jupyter-wire client. One instance per operation is fine; all durable state is on disk."""
import json
import time
from dataclasses import dataclass

from .cells import CellRecord, read_output_since, read_record
from .paths import Config, kernel_dir
from .venv import venv_python  # noqa: F401  (imported for kernel spawn parity)


@dataclass
class Completed:
    cell_id: int
    record: CellRecord
    output: str


@dataclass
class Running:
    cell_id: int
    output: str
    next_offset: int


@dataclass
class Busy:
    cell_id: int | None


class KernelClient:
    def __init__(self, key: str):
        self.key = key

    def _connect(self):
        from jupyter_client import BlockingKernelClient
        kc = BlockingKernelClient()
        kc.load_connection_file(str(kernel_dir(self.key) / "connection.json"))
        kc.start_channels()
        return kc

    def _await_cell_id(self, kc, msg_id: str, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = kc.get_iopub_msg(timeout=max(deadline - time.monotonic(), 0.1))
            except Exception:
                continue
            if (msg.get("parent_header", {}).get("msg_id") == msg_id
                    and msg["header"]["msg_type"] == "execute_input"):
                return int(msg["content"]["execution_count"])
        raise TimeoutError("kernel never acknowledged the cell (no execute_input)")

    def _follow(self, cell_id: int, timeout_s: float) -> Completed | Running:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                out, off = read_output_since(self.key, cell_id, 0)
                return Completed(cell_id, rec, out)
            time.sleep(0.2)
        out, off = read_output_since(self.key, cell_id, 0)
        return Running(cell_id, out, off)

    def exec_cell(self, code: str, timeout_s: float, config: Config) -> Completed | Running | Busy:
        kc = self._connect()
        try:
            msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
            cell_id = self._await_cell_id(kc, msg_id)
        finally:
            kc.stop_channels()
        return self._follow(cell_id, timeout_s)


def run_bootstrap(key: str, config: Config) -> None:
    """NOTE (T6): once exec_cell requires kernel-side current.json confirmation, the
    bootstrap cell cannot use it (the hooks it waits on are installed BY that very
    cell). run_bootstrap therefore submits directly and follows the shell-channel
    execute_reply instead — see _exec_raw below, added in T6."""
    payload = json.dumps({
        "key": key,
        "kernel_dir": str(kernel_dir(key)),
        "idle_hours": config.idle_hours,
        "max_concurrency": config.max_concurrency,
        "depth": config.depth,
        "max_depth": config.max_depth,
    })
    code = f"import ptc.runtime.bootstrap as _ptc_b; _ptc_b.install({payload!r})"
    out = KernelClient(key).exec_cell(code, timeout_s=60, config=config)
    if isinstance(out, Completed) and out.record.status == "ok":
        return
    detail = getattr(getattr(out, "record", None), "error", None) or getattr(out, "output", "")
    raise RuntimeError(f"ptc bootstrap failed in kernel {key}: {detail}")

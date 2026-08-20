"""Jupyter-wire client. One instance per operation is fine; all durable state is on disk."""
import json
import os
import signal
import time
from dataclasses import dataclass

from .cells import (
    CellRecord,
    current_cell,
    default_offset,
    read_output_since,
    read_record,
    save_offset,
)
from .kernel import kernel_alive
from .lock import submit_lock
from .ownership import read_owner
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
    #: why admission was refused — "running", "pending-unconfirmed", "lock-held"
    reason: str = ""


def _epoch_ended_record() -> dict:
    """The settle for a cell whose kernel epoch ended before it did (F3)."""
    return {"status": "error", "duration_ms": 0, "result_repr": None,
            "error": {"ename": "KernelEpochEnded",
                      "evalue": "the kernel restarted before this cell finished",
                      "traceback": ""},
            "images": [], "mutations": []}


class KernelClient:
    def __init__(self, key: str):
        self.key = key

    def _connect(self, **channels):
        from jupyter_client import BlockingKernelClient
        kc = BlockingKernelClient()
        kc.load_connection_file(str(kernel_dir(self.key) / "connection.json"))
        kc.start_channels(**channels)
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
        # seed the cursor sidecar: this output has been handed to the caller, so a
        # later cursorless wait_cell must resume after it instead of replaying it
        save_offset(self.key, cell_id, off)
        return Running(cell_id, out, off)

    # -- busy model (F2): a kernel is busy when the kernel-side current.json names a
    # cell with no terminal record, OR when a submitted request has not yet been
    # confirmed (pending.json, left behind by any failure inside the window where a
    # request is on the wire but current.json does not name it yet).
    def is_busy(self) -> Busy | None:
        cur = current_cell(self.key)
        if cur is not None and read_record(self.key, cur) is None and kernel_alive(self.key):
            return Busy(cur, reason="running")
        pend = kernel_dir(self.key) / "cells" / "pending.json"
        try:
            data = json.loads(pend.read_text())
            if time.time() - data.get("submitted_at", 0) < 60 and kernel_alive(self.key):
                # busy admitting an unacknowledged cell; the marker names it when the
                # kernel got as far as execute_input, else nobody knows its id yet
                cid = data.get("cell_id")
                return Busy(cid if cid is not None else -1, reason="pending-unconfirmed")
            pend.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def _mark_pending(self, msg_id: str, cell_id: int | None) -> None:
        """Record a request that is on the wire but unconfirmed. Best effort: a marker
        we cannot write must not mask the fault that sent us here."""
        try:
            pend = kernel_dir(self.key) / "cells" / "pending.json"
            pend.parent.mkdir(parents=True, exist_ok=True)
            pend.write_text(json.dumps(
                {"msg_id": msg_id, "cell_id": cell_id, "submitted_at": time.time()}))
        except OSError:
            pass

    def exec_cell(self, code: str, timeout_s: float, config: Config) -> Completed | Running | Busy:
        """Atomic admission (F2): the submit lock is held from the busy check until the
        kernel-side pre_run_cell has published current.json for OUR cell. A loser that
        times out on the lock is told busy — a submitted request is never silently queued."""
        try:
            lock_cm = submit_lock(self.key)
            lock_cm.__enter__()
        except TimeoutError:
            held = self.is_busy()
            return Busy(held.cell_id if held is not None else None, reason="lock-held")
        try:
            busy = self.is_busy()
            if busy is not None:
                return busy
            kc = self._connect()
            try:
                msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
                # sent-unacknowledged window: the request is on the wire and nothing on
                # disk records it. EVERY exit from here until current.json names our
                # cell must fail closed with a marker — a lost acknowledgement, a
                # wedged kernel, a Ctrl-C — or the next caller admits a second cell on
                # top of one this kernel may still be about to run (F2).
                cell_id = None
                try:
                    cell_id = self._await_cell_id(kc, msg_id)
                    deadline = time.monotonic() + 15.0
                    while current_cell(self.key) != cell_id:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                f"kernel {self.key} accepted cell {cell_id} but never "
                                "published it; the kernel may be wedged — interrupt() or "
                                "restart()")
                        time.sleep(0.02)
                except BaseException:
                    self._mark_pending(msg_id, cell_id)
                    raise
                (kernel_dir(self.key) / "cells" / "pending.json").unlink(missing_ok=True)
            finally:
                kc.stop_channels()
        finally:
            lock_cm.__exit__(None, None, None)
        return self._follow(cell_id, timeout_s)

    def _archived(self, cell_id: int, offset: int = 0) -> Completed | None:
        """A cell id from a previous kernel epoch (F3): settle it from the archive,
        resuming at the caller's cursor so an already-delivered log is not replayed."""
        for d in sorted(kernel_dir(self.key).glob("cells-prev-*"), reverse=True):
            log = d / f"{cell_id}.log"
            if not log.exists():
                continue
            raw = log.read_bytes()
            text = raw[max(offset, 0):].decode(errors="replace")
            save_offset(self.key, cell_id, len(raw))
            try:
                rec = json.loads((d / f"{cell_id}.json").read_text())
                rec.setdefault("error", None)
                record = CellRecord(**rec)
            except (OSError, json.JSONDecodeError, TypeError, AttributeError):
                # missing, unreadable, or written to a schema this build does not
                # know: an archived cell must still settle, never crash the wait
                record = CellRecord(**_epoch_ended_record())
            note = f"\n[cell {cell_id} belongs to a previous kernel epoch — archived at {d}]"
            return Completed(cell_id, record, text + note)
        return None

    def wait_cell(self, cell_id: int, timeout_s: float, since: int = -1) -> Completed | Running:
        offset = default_offset(self.key, cell_id) if since < 0 else since
        deadline = time.monotonic() + timeout_s
        while True:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                text, new_off = read_output_since(self.key, cell_id, offset)
                save_offset(self.key, cell_id, new_off)
                return Completed(cell_id, rec, text)
            if not (kernel_dir(self.key) / "cells" / f"{cell_id}.log").exists():
                arch = self._archived(cell_id, offset)
                if arch is not None:
                    return arch
            if not kernel_alive(self.key):
                text, new_off = read_output_since(self.key, cell_id, offset)
                dead = CellRecord(status="error", duration_ms=0, result_repr=None,
                                  error={"ename": "KernelDied",
                                         "evalue": "kernel process died before the cell finished",
                                         "traceback": ""},
                                  images=[], mutations=[])
                return Completed(cell_id, dead, text)
            if time.monotonic() >= deadline:
                text, new_off = read_output_since(self.key, cell_id, offset)
                save_offset(self.key, cell_id, new_off)
                return Running(cell_id, text, new_off)
            time.sleep(0.2)

    def interrupt(self) -> None:
        """interrupt_request on the control channel, then SIGINT after a 2 s grace."""
        try:
            # No heartbeat: hb_channel.start() spawns a thread that creates its socket
            # only once it is scheduled, and this client is torn down moments later —
            # stop_channels() ends in context.destroy(), so that thread loses the race
            # and dies with "ZMQError: Too many open files" on stderr.
            kc = self._connect(hb=False)
            try:
                msg = kc.session.msg("interrupt_request", {})
                kc.control_channel.send(msg)
                # Block on interrupt_reply before tearing down. context.destroy()
                # discards whatever is still queued, and a freshly connected DEALER
                # has nothing flushed yet, so an unawaited request is a race the
                # sender loses — every interrupt would fall through to SIGINT.
                kc.get_control_msg(timeout=2.0)
            finally:
                kc.stop_channels()
        except Exception:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.is_busy() is None:
                return
            time.sleep(0.1)
        o = read_owner(self.key)
        if o:
            try:
                os.kill(o.pid, signal.SIGINT)
            except OSError:
                pass

    def _exec_raw(self, code: str, timeout_s: float) -> Completed | Running:
        """Bootstrap-only path: no submit lock, no current.json wait (the hooks are
        installed by the very cell this runs). Follows the shell execute_reply."""
        kc = self._connect()
        try:
            msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
            cell_id = self._await_cell_id(kc, msg_id)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    reply = kc.get_shell_msg(timeout=1.0)
                except Exception:
                    continue
                if reply.get("parent_header", {}).get("msg_id") == msg_id:
                    ok = reply["content"].get("status") == "ok"
                    text, _ = read_output_since(self.key, cell_id, 0)
                    rec = read_record(self.key, cell_id) or CellRecord(
                        status="ok" if ok else "error", duration_ms=0, result_repr=None,
                        error=None if ok else {
                            "ename": reply["content"].get("ename", "Error"),
                            "evalue": reply["content"].get("evalue", ""),
                            # the reply carries the only traceback there is: a bootstrap
                            # that dies before registering post_run_cell writes no record
                            "traceback": "\n".join(
                                reply["content"].get("traceback", []))[-8000:]},
                        images=[], mutations=[])
                    return Completed(cell_id, rec, text)
            return Running(cell_id, "", 0)
        finally:
            kc.stop_channels()


def run_bootstrap(key: str, config: Config) -> None:
    """The bootstrap cell cannot go through exec_cell: that path waits for kernel-side
    current.json confirmation, and the hooks that publish it are installed BY this very
    cell. _exec_raw submits directly and follows the shell-channel execute_reply, so a
    failure surfaces as soon as the kernel replies, carrying the reply's traceback."""
    payload = json.dumps({
        "key": key,
        "kernel_dir": str(kernel_dir(key)),
        "idle_hours": config.idle_hours,
        "max_concurrency": config.max_concurrency,
        "depth": config.depth,
        "max_depth": config.max_depth,
    })
    code = f"import ptc.runtime.bootstrap as _ptc_b; _ptc_b.install({payload!r})"
    out = KernelClient(key)._exec_raw(code, timeout_s=60)
    if isinstance(out, Completed) and out.record.status == "ok":
        return
    if isinstance(out, Running):
        detail = "kernel never replied to the bootstrap cell within 60s"
    else:
        detail = out.record.error or out.output or f"status={out.record.status}"
    raise RuntimeError(f"ptc bootstrap failed in kernel {key}: {detail}")

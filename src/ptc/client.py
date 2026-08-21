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
from .paths import Config, kernel_dir, private_write_text, secure_dir
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


@dataclass
class NotFound:
    """No such cell under this key: no record, no log, no archive, and a live kernel.

    PTC never queues a cell and a confirmed one has its log before any caller can learn
    its number, so this state cannot become valid later — waiting on it is waiting on
    nothing. A mistyped id and an id from another session both land here.
    """
    cell_id: int


def _epoch_ended_record() -> dict:
    """The settle for a cell whose kernel epoch ended before it did (F3)."""
    return {"status": "error", "duration_ms": 0, "result_repr": None,
            "error": {"ename": "KernelEpochEnded",
                      "evalue": "the kernel restarted before this cell finished",
                      "traceback": ""},
            "images": [], "mutations": []}


def _kernel_died_record() -> dict:
    """The settle for a cell whose kernel process died while it was still running."""
    return {"status": "error", "duration_ms": 0, "result_repr": None,
            "error": {"ename": "KernelDied",
                      "evalue": "kernel process died before the cell finished",
                      "traceback": ""},
            "images": [], "mutations": []}


def _rebased_images(images: list, d) -> list:
    """Recorded image paths, remapped onto the archive directory that now holds them.

    A record names its images by the path they had under cells/, and restart MOVED those
    files into cells-prev-*. The renderer drops paths that no longer exist, so an archived
    wait silently lost every image the cell had produced. Only files really present in the
    archive survive the remap — a missing one is dropped exactly as before.
    """
    out = []
    for p in images:
        q = d / os.path.basename(str(p))
        if q.exists():
            out.append(str(q))
    return out


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

    def _settle_dead(self, cell_id: int, offset: int) -> Completed:
        """The kernel is gone: settle the cell from whatever it left behind.

        The record is re-read first — the settle may have landed in the window between the
        caller's last look and the kernel's death — and only with none is KernelDied the
        verdict. The cursor is deliberately NOT advanced: a dead kernel writes no more
        output, so there is nothing to resume after.
        """
        rec = read_record(self.key, cell_id) or CellRecord(**_kernel_died_record())
        text, _ = read_output_since(self.key, cell_id, offset)
        return Completed(cell_id, rec, text)

    def _follow(self, cell_id: int, timeout_s: float) -> Completed | Running:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                out, off = read_output_since(self.key, cell_id, 0)
                return Completed(cell_id, rec, out)
            if not kernel_alive(self.key):
                # The cell took the kernel down with it — os._exit, a segfault, an OOM
                # kill. Polling for a record nothing can write any more burns the whole
                # yield budget (300 s by default) and then calls the cell Running, which
                # is false twice over. wait_cell has answered this correctly since F3.
                return self._settle_dead(cell_id, 0)
            time.sleep(0.2)
        out, off = read_output_since(self.key, cell_id, 0)
        # seed the cursor sidecar: this output has been handed to the caller, so a
        # later cursorless wait_cell must resume after it instead of replaying it
        save_offset(self.key, cell_id, off)
        return Running(cell_id, out, off)

    # -- busy model (F2): a kernel is busy when the kernel-side current.json names a
    # cell with no terminal record, OR when a submitted request has not yet been
    # confirmed (pending.json, written before the send and cleared only once current.json
    # names our cell, so it covers the whole wire window including the submitter's death
    # inside it).
    def is_busy(self) -> Busy | None:
        cur = current_cell(self.key)
        if cur is not None and read_record(self.key, cur) is None and kernel_alive(self.key):
            return Busy(cur, reason="running")
        pend = kernel_dir(self.key) / "cells" / "pending.json"
        try:
            data = json.loads(pend.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            pend.unlink(missing_ok=True)
            return None
        if self._pending_discharged(data):
            pend.unlink(missing_ok=True)
            return None
        # busy admitting an unacknowledged cell; the marker names it when the kernel got
        # as far as execute_input, else nobody knows its id yet
        cid = data.get("cell_id")
        return Busy(cid if cid is not None else -1, reason="pending-unconfirmed")

    def _pending_discharged(self, data: dict) -> bool:
        """Durable evidence that a sent-but-unconfirmed request can no longer execute.

        AGE is not evidence. The marker covers the case where the kernel accepted an
        execute_request that never reached current.json, and such a request may still be
        queued behind a wedged cell however old it is — deleting it on a stopwatch admits
        a second cell on top of one the kernel is still about to run, which is the one
        guarantee this whole path exists to keep: nothing is ever silently queued.

        Three things do discharge it, and only these three:
          * the kernel that took the request is gone, or came back under a new epoch —
            whatever it accepted died with it;
          * a terminal record exists for the cell the marker names — that cell settled;
          * current.json names a LATER cell — the kernel runs cells in order, so a later
            one can only have started after ours left the queue.
        With none of them the verdict stays busy. The escape hatch for a request whose id
        was never learned is restart(), which archives cells/ and the marker with it.
        """
        if not kernel_alive(self.key):
            return True
        o = read_owner(self.key)
        if data.get("epoch") and o is not None and o.epoch != data["epoch"]:
            return True
        cid = data.get("cell_id")
        if cid is None:
            return False
        if read_record(self.key, cid) is not None:
            return True
        cur = current_cell(self.key)
        return cur is not None and cur > cid

    def _owner_identity(self) -> tuple | None:
        """Which kernel INCARNATION owns this key right now.

        The epoch is a whole-second timestamp, so two restarts inside one second share
        one; the nonce is drawn per spawn. The pair is what tells two incarnations apart.
        """
        o = read_owner(self.key)
        return (o.epoch, o.nonce) if o else None

    def _mark_pending(self, msg_id: str | None, cell_id: int | None,
                      owner: tuple | None) -> None:
        """Record a request that is about to go on the wire, for the incarnation `owner`.

        This one is written BEFORE the send and IS the admission guard, so it raises: a
        marker that cannot be written aborts the submission rather than letting it proceed
        unguarded. The epoch is stamped in so a later kernel can tell the marker is not
        about IT (see `_pending_discharged`).
        """
        secure_dir(kernel_dir(self.key) / "cells")
        private_write_text(
            kernel_dir(self.key) / "cells" / "pending.json",
            json.dumps({"msg_id": msg_id, "cell_id": cell_id,
                        "submitted_at": time.time(),
                        "epoch": owner[0] if owner else None}))

    def _refresh_pending(self, msg_id: str | None, cell_id: int | None,
                         owner: tuple | None) -> None:
        """Update the marker with what the failed send learned — but only while the kernel
        we submitted TO still owns this key.

        A restart during an in-flight exec replaces the kernel underneath the submitter:
        cells/ is archived, marker and all, and that archiving IS the settlement for
        everything the old kernel was holding. Writing here afterwards plants a marker in
        the REPLACEMENT's cells/ under the replacement's epoch, for a request the
        replacement never took — and one whose cell id was never learned can then never be
        discharged, so the fresh kernel is permanently busy from the moment it is ready.
        The old request died with the old kernel; the restart already answered for it.

        Never raises: this runs from a failure handler and must not mask the fault that
        sent us there.
        """
        if self._owner_identity() != owner:
            return
        try:
            self._mark_pending(msg_id, cell_id, owner)
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
                # The marker goes on disk BEFORE the request goes on the wire. An
                # exception handler cannot cover the window that matters: a SIGKILL landing
                # between the send and the handler leaves no marker at all, and process
                # death releases the submit lock — so the next adapter sees neither
                # current.json nor a marker while this kernel still holds the request, and
                # silently queues a second cell on top of it. Nothing has been sent yet, so
                # a marker that cannot be written aborts the submission (OSError out of
                # here) rather than sending unguarded.
                # The incarnation is captured HERE, with the marker, and every later write
                # is checked against it: a restart can replace the kernel while this
                # request is in flight, and a marker written into the replacement's
                # cells/ is one the replacement can never discharge (_refresh_pending).
                owner = self._owner_identity()
                self._mark_pending(None, None, owner)
                # sent-unacknowledged window: the request is on the wire and only the
                # marker records it. EVERY exit from here until current.json names our
                # cell must fail closed — a lost acknowledgement, a wedged kernel, a
                # Ctrl-C — or the next caller admits a second cell on top of one this
                # kernel may still be about to run (F2).
                msg_id = cell_id = None
                try:
                    msg_id = kc.execute(code, store_history=True, allow_stdin=False,
                                        stop_on_error=False)
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
                    # Refresh the marker with whatever was learned — a marker naming the
                    # cell is dischargeable by that cell's record, one naming nothing only
                    # by the kernel's death or by restart(). It is never REMOVED here: an
                    # exception out of execute() does not prove the request stayed off the
                    # wire, and only proof discharges the marker (`_pending_discharged`).
                    self._refresh_pending(msg_id, cell_id, owner)
                    raise
                # Same rule clearing it: the marker this submission owns went into the
                # archive with its own cells/ if the kernel was replaced, and the file
                # standing here now would belong to the replacement's submitter.
                if self._owner_identity() == owner:
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
                record.images = _rebased_images(record.images, d)
            except (OSError, json.JSONDecodeError, TypeError, AttributeError):
                # missing, unreadable, or written to a schema this build does not
                # know: an archived cell must still settle, never crash the wait
                record = CellRecord(**_epoch_ended_record())
            note = f"\n[cell {cell_id} belongs to a previous kernel epoch — archived at {d}]"
            return Completed(cell_id, record, text + note)
        return None

    def _pending_names(self, cell_id: int) -> bool:
        """Is this the cell the sent-but-unconfirmed marker names?

        The kernel acknowledged it (execute_input carried the number) but has not started
        it — it is queued behind a wedged cell — so it has no log of its own yet. That id
        is known to be real, which is exactly what separates it from an unknown one.
        """
        try:
            data = json.loads((kernel_dir(self.key) / "cells" / "pending.json").read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and data.get("cell_id") == cell_id

    def wait_cell(self, cell_id: int, timeout_s: float,
                  since: int = -1) -> Completed | Running | NotFound:
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
                # Nothing under this key ever ran this cell: no record, no log, no
                # archive. A live kernel used to make the loop report Running after every
                # timeout, forever — but PTC never queues a cell, and a confirmed one has
                # its log before any caller can learn its number, so this can only be a
                # mistyped id or one from another session and no later moment makes it
                # valid. The one exception is a cell the kernel acknowledged and has not
                # started yet; the marker names that one, and it is genuinely pending.
                if kernel_alive(self.key) and not self._pending_names(cell_id):
                    return NotFound(cell_id)
            if not kernel_alive(self.key):
                return self._settle_dead(cell_id, offset)
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

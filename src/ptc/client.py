"""Jupyter-wire client. One instance per operation is fine; all durable state is on disk."""
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .cells import (
    CellRecord,
    current_cell,
    default_offset,
    offset_name,
    offset_path,
    read_output_since,
    read_record,
    read_since,
    save_offset,
)
from .kernel import kernel_alive
from .lock import submit_lock
from .ownership import (
    UnknownOwner,
    read_owner,
    settled_owner_state,
    start_time_matches,
)
from .paths import Config, kernel_dir, private_write_text, secure_dir
from .venv import venv_python  # noqa: F401  (imported for kernel spawn parity)


@dataclass
class Completed:
    cell_id: int
    record: CellRecord
    output: str
    #: Where this cell's output really lives, when that is NOT `cells/<id>.log`. A cell
    #: settled from a previous epoch was read out of `cells-prev-<ts>/`, and the renderer's
    #: truncation notice and the CLI's `full_log` both pointed at the live path instead —
    #: sending whoever needed the untruncated output to a file that does not exist.
    log_path: Path | None = None


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


#: How long a submission waits for its connection to be provably synchronized with the
#: kernel (`_connect_to_current_owner`). Matches the readiness budget `ensure_kernel` gives
#: the same round trip at spawn: a kernel that cannot answer kernel_info inside it is not
#: one this submission should be sending to.
_READY_S = 20.0


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

    def _await_cell_id(self, kc, msg_id: str, baseline: int | None,
                       timeout: float = 15.0) -> int:
        """Which cell number the kernel gave this request — from iopub, or from disk.

        iopub is the primary witness and the one that can go missing. `start_channels()`
        returns before the SUB socket's subscription has reached the kernel's PUB, so a
        fast cell publishes its `execute_input` into that gap and this loop waits out its
        whole budget for a message that was already sent to nobody. The submission then
        failed with a marker naming cell_id None — undischargeable while the kernel lives
        (`_pending_discharged`), so every later submission was Busy until a restart. The
        handshake is now waited out before the send (`_connect_to_current_owner`); this is
        the second witness for the case that survives it.

        The kernel-side pre_run_cell publishes the same number to current.json, on disk,
        over no socket at all. `baseline` is what that file named BEFORE this request went
        out, read by the caller under the same lock it still holds — so a strictly newer id
        standing there cannot belong to anyone else's submission, and equality is not
        enough (it would re-attribute the previous cell to this request).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur = current_cell(self.key)
            if cur is not None and (baseline is None or cur > baseline):
                return cur
            try:
                msg = kc.get_iopub_msg(timeout=min(0.2, max(deadline - time.monotonic(),
                                                            0.05)))
            except Exception:
                continue
            if (msg.get("parent_header", {}).get("msg_id") == msg_id
                    and msg["header"]["msg_type"] == "execute_input"):
                return int(msg["content"]["execution_count"])
        raise TimeoutError("kernel never acknowledged the cell (no execute_input, and "
                           "current.json never named a new one)")

    def _kernel_known_dead(self) -> bool:
        """PROOF that nothing can still be running under this key — never a guess.

        `kernel_alive()` collapses an identity it could not read to "not alive", which is
        the right pessimism for a row in `ptc list` and the wrong one here: every caller
        below DISCHARGES something on this verdict — the admission marker, a running
        cell's settlement — so one transient `ps`/libproc failure against a LIVE kernel
        threw away the guard that keeps a second cell off an in-flight one, or settled a
        running cell as KernelDied. The question is therefore inverted: unknown reads as
        "still there", and the caller stays busy or keeps polling until the identity can
        be read again (poll loops ask afresh 0.2 s later; `wait_cell`'s deadline still
        returns an honest Running).

        Costs what `kernel_alive` cost in the reachable case — `settled_owner_state`
        retries only the read that came back unknown — so the 0.2 s poll loops are
        unchanged except in the failure they exist to survive. The absent `ready` file and
        the absent `owner.json` are proof of their own: no identity read is involved.
        """
        o = read_owner(self.key)
        if o is None:
            return True
        try:
            if not settled_owner_state(o):
                return True
        except UnknownOwner:
            return False
        return not (kernel_dir(self.key) / "ready").exists()

    def _settle_dead(self, cell_id: int, offset: int, *, implicit: bool) -> Completed:
        """No kernel is answering: settle the cell from whatever it left behind.

        Three places are consulted in the order that keeps the most truth. The live record
        first — the settle may have landed in the window between the caller's last look and
        the kernel's death. Then the ARCHIVE, but only when this cell's live log is gone:
        "no kernel" also describes a RESTART, and a restart moves this cell's log and record
        into `cells-prev-*` before the replacement comes up, so a completed cell is sitting
        right there and calling it KernelDied would hide it. Only with neither is KernelDied
        the verdict.

        The live log is what makes that archive consult about THIS cell. Execution counts
        restart at 1 in every epoch, so `cells-prev-*` holds ids the current kernel hands
        out again, and an unguarded lookup settles a cell that died mid-run with a
        same-numbered stranger from an older epoch — its status, its output, its images,
        and no KernelDied anywhere. A restart is precisely what MOVES the log out of
        `cells/`, so one still standing there proves the id belongs to the epoch that just
        died and nothing in the archive is about it. (`_follow` and `wait_cell` guard their
        own in-loop consults on the same fact.)

        On the first and last paths the cursor is deliberately NOT advanced: a dead kernel
        writes no more output, so there is nothing to resume after. `_archived` advances
        it, as it does for every other caller — it has just handed the archived log over.
        """
        rec = read_record(self.key, cell_id)
        if rec is not None:
            text, _ = read_output_since(self.key, cell_id, offset)
            return Completed(cell_id, rec, text)
        if not (kernel_dir(self.key) / "cells" / f"{cell_id}.log").exists():
            arch = self._archived(cell_id, offset, implicit=implicit)
            if arch is not None:
                return arch
        text, _ = read_output_since(self.key, cell_id, offset)
        return Completed(cell_id, CellRecord(**_kernel_died_record()), text)

    def _follow(self, cell_id: int, timeout_s: float) -> Completed | Running:
        deadline = time.monotonic() + timeout_s
        live_log = kernel_dir(self.key) / "cells" / f"{cell_id}.log"
        while time.monotonic() < deadline:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                out, off = read_output_since(self.key, cell_id, 0)
                arch = self._rotated_under_the_record(cell_id, 0, out, implicit=True)
                if arch is not None:
                    return arch
                # Same rule as the Running exit below, and it applies to a cell that
                # finished inside the yield just as much: this output HAS been handed to
                # the caller. Dropping the offset here left the sidecar unseeded, so a
                # later `wait(cell_id=…)` with no `since` from this same adapter replayed
                # the whole cell — while `since=-1` is documented as "resume after what
                # this adapter last served".
                save_offset(self.key, cell_id, off)
                return Completed(cell_id, rec, out)
            if not live_log.exists():
                # Another client restarted the kernel under this cell: its log and its
                # record went into `cells-prev-*` with the epoch that ran them. The
                # REPLACEMENT kernel keeps kernel_alive() true, so the check below never
                # fires and this loop would poll a directory that can never answer again,
                # spend the whole yield budget and then call a finished cell Running.
                # (The log is also simply absent for the instant before the kernel opens
                # it, which is why a miss here just keeps polling.)
                arch = self._archived(cell_id, 0, implicit=True)
                if arch is not None:
                    return arch
            if self._kernel_known_dead():
                # The cell took the kernel down with it — os._exit, a segfault, an OOM
                # kill. Polling for a record nothing can write any more burns the whole
                # yield budget (300 s by default) and then calls the cell Running, which
                # is false twice over. wait_cell has answered this correctly since F3.
                # PROOF of death, not `kernel_alive`'s absence of proof of life: settling
                # a running cell as KernelDied because one identity read failed is a
                # terminal answer invented out of nothing (`_kernel_known_dead`).
                return self._settle_dead(cell_id, 0, implicit=True)
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
        if cur is not None and read_record(self.key, cur) is None \
                and not self._kernel_known_dead():
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
          * the kernel that took the request is PROVABLY gone, or came back as a different
            INCARNATION — whatever it accepted died with it. Incarnation is the epoch and
            the nonce together (`_owner_identity`): the epoch is a whole-second timestamp,
            so a restart inside one second leaves the marker of the kernel it replaced
            looking exactly like its own, and a marker naming no cell has nothing else that
            could ever discharge it. A marker written before nonces were stamped carries
            none, and an absent key is absence of evidence rather than a mismatch;
          * a terminal record exists for the cell the marker names — that cell settled;
          * current.json names a LATER cell — the kernel runs cells in order, so a later
            one can only have started after ours left the queue.
        With none of them the verdict stays busy. The escape hatch for a request whose id
        was never learned is restart(), which archives cells/ and the marker with it.

        "Provably gone" is the whole of the first one. This opened with `kernel_alive()`,
        whose unknown reads as dead, and the caller UNLINKS the marker on a discharge — so
        a single unreadable identity on a live kernel admitted a second cell on top of the
        one it was still running (`_kernel_known_dead`).
        """
        if self._kernel_known_dead():
            return True
        o = read_owner(self.key)
        if o is not None:
            if data.get("epoch") and o.epoch != data["epoch"]:
                return True
            if data.get("nonce") and o.nonce != data["nonce"]:
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

    def _connect_to_current_owner(self):
        """Connect, and return the connection WITH the incarnation it really reached.

        `connection.json` and `owner.json` are two files a restart rewrites one after the
        other, and reading them in either order across a restart pairs a connection with
        an incarnation that does not own it. The dangerous half is the marker: an owner
        read AFTER the restart names the REPLACEMENT, so the pending marker goes down
        stamped with the replacement's epoch while the request goes out on the OLD
        kernel's ports. That request times out unanswered, nothing under the replacement
        can ever discharge a marker naming no cell (`_pending_discharged`), and the fresh
        kernel is busy for good. The other order is harmless by comparison — a marker
        stamped with an epoch the current kernel does not share discharges on sight.

        So the pair is bound instead of assumed: read the owner, connect, read it again,
        and accept only when the two reads agree — which means no incarnation change
        straddled the connect, so the ports belong to that owner. A restart landing in the
        window costs one retry; two in a row is a key being replaced faster than anything
        can bind to it, and that is reported rather than papered over.

        The connection is also SYNCHRONIZED before it is accepted. `start_channels()`
        returns as soon as the sockets are created, and a ZeroMQ SUB subscription reaches
        the kernel's PUB some time after that — so a cell submitted immediately could
        publish its `execute_input` before anyone was subscribed to hear it, and the
        submission that lost that race left a marker nothing could discharge (see
        `_await_cell_id`). `wait_for_ready` is jupyter_client's own name for closing that
        gap: a kernel_info round trip on shell, then iopub drained until it answers, which
        is precisely the proof that the subscription is live. It runs BEFORE anything is
        sent and before the marker is written, so its failure is an ordinary
        connection failure — nothing on the wire, nothing on disk to clean up.
        """
        for attempt in (0, 1):
            owner = self._owner_identity()
            kc = self._connect()
            try:
                kc.wait_for_ready(timeout=_READY_S)
            except BaseException:
                kc.stop_channels()
                raise
            if self._owner_identity() == owner:
                return kc, owner
            kc.stop_channels()
            if attempt == 0:
                time.sleep(0.2)
        raise RuntimeError(
            f"kernel {self.key} was replaced twice while this submission was binding to "
            "it — the owner changed between reading its connection file and connecting. "
            "Nothing was sent; retry once the restarts have settled.")

    def _mark_pending(self, msg_id: str | None, cell_id: int | None,
                      owner: tuple | None) -> None:
        """Record a request that is about to go on the wire, for the incarnation `owner`.

        This one is written BEFORE the send and IS the admission guard, so it raises: a
        marker that cannot be written aborts the submission rather than letting it proceed
        unguarded. The whole incarnation is stamped in — epoch AND nonce — so a later
        kernel can tell the marker is not about IT (see `_pending_discharged`); the epoch
        alone resolves only to the second, which two restarts can share.
        """
        secure_dir(kernel_dir(self.key) / "cells")
        private_write_text(
            kernel_dir(self.key) / "cells" / "pending.json",
            json.dumps({"msg_id": msg_id, "cell_id": cell_id,
                        "submitted_at": time.time(),
                        "epoch": owner[0] if owner else None,
                        "nonce": owner[1] if owner else None}))

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
            # The incarnation is captured as part of CONNECTING, not after it: an owner
            # read once the ports are already open can name a kernel that replaced the one
            # this connection reaches (`_connect_to_current_owner`).
            kc, owner = self._connect_to_current_owner()
            try:
                # The marker goes on disk BEFORE the request goes on the wire. An
                # exception handler cannot cover the window that matters: a SIGKILL landing
                # between the send and the handler leaves no marker at all, and process
                # death releases the submit lock — so the next adapter sees neither
                # current.json nor a marker while this kernel still holds the request, and
                # silently queues a second cell on top of it. Nothing has been sent yet, so
                # a marker that cannot be written aborts the submission (OSError out of
                # here) rather than sending unguarded.
                # Every later write is checked against `owner`: a restart can replace the
                # kernel while this request is in flight, and a marker written into the
                # replacement's cells/ is one the replacement can never discharge
                # (_refresh_pending).
                self._mark_pending(None, None, owner)
                # sent-unacknowledged window: the request is on the wire and only the
                # marker records it. EVERY exit from here until current.json names our
                # cell must fail closed — a lost acknowledgement, a wedged kernel, a
                # Ctrl-C — or the next caller admits a second cell on top of one this
                # kernel may still be about to run (F2).
                msg_id = cell_id = None
                # What current.json named before this request existed. The submit lock has
                # been held since the busy check and is held until our cell is published,
                # so nothing else can advance that number in between — which is what makes
                # a strictly newer one proof of OUR cell (`_await_cell_id`).
                baseline = current_cell(self.key)
                try:
                    msg_id = kc.execute(code, store_history=True, allow_stdin=False,
                                        stop_on_error=False)
                    cell_id = self._await_cell_id(kc, msg_id, baseline)
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

    def _archived(self, cell_id: int, offset: int = 0, *,
                  implicit: bool = True) -> Completed | None:
        """A cell id from a previous kernel epoch (F3): settle it from the archive,
        resuming at the caller's cursor so an already-delivered log is not replayed.

        `implicit` is the caller's `since` form, not its offset: True for the cursorless
        request every exec-path follow makes, False when the caller named a byte
        (`_archived_start`). It defaults to the implicit form because that is what the
        API's own default (`since=-1`) is.

        Bounded like every other log read (`read_since`), and for the same reason: an
        archived log has no size of its own — a verbose cell leaves gigabytes behind — and
        this ran in a CLI or an MCP adapter that would hold all of it before the renderer
        truncated it to a few thousand characters. The cursor returns pointing just past
        what was read and `log_path` names the file, so the rest stays reachable.
        """
        for d in sorted(kernel_dir(self.key).glob("cells-prev-*"), reverse=True):
            log = d / f"{cell_id}.log"
            if not log.exists():
                continue
            text, new_off = read_since(
                log, self._archived_start(d, cell_id, offset, implicit))
            save_offset(self.key, cell_id, new_off)
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
            return Completed(cell_id, record, text + note, log_path=log)
        return None

    def _rotated_under_the_record(self, cell_id: int, offset: int, text: str, *,
                                  implicit: bool) -> "Completed | None":
        """Did a restart move this cell out from between the record read and the log read?

        The record branch is two reads, and a concurrent restart fits between them: the
        record comes back, `cells/` is renamed to `cells-prev-*`, and the log read then
        opens nothing. The caller got a TERMINAL Completed carrying the record's status and
        no output at all — plus `record.images` naming paths in a directory that has moved,
        which the renderer drops silently. The cell finished and everything it produced was
        sitting in the archive.

        Both facts are required before re-settling, because either alone is ordinary: a
        cell that genuinely printed nothing is empty for good reasons, and a cursor already
        past the end of a live log reads empty every time. Only empty AND no live log at
        all describes the rotation. `_archived` then reads the archived log and rebases the
        image paths onto the directory that now holds them; an archive with nothing to say
        (a cell whose id the CURRENT epoch handed out, whose log was never rotated) leaves
        the caller with exactly what the live read gave.

        Each caller passes the cursor its own branch uses — `wait_cell` resumes at the
        caller's offset, `_follow` reads the whole cell — and `_archived` advances the
        sidecar as it does for every other archived read.
        """
        if text:
            return None
        if (kernel_dir(self.key) / "cells" / f"{cell_id}.log").exists():
            return None
        return self._archived(cell_id, offset, implicit=implicit)

    def _archived_start(self, d: Path, cell_id: int, offset: int, implicit: bool) -> int:
        """Where an archived read really starts for THIS caller.

        A restart renames `cells/` — every caller's cursor sidecar inside it — and puts a
        fresh empty `cells/` in its place, so `default_offset` answers 0 for a long-lived
        adapter that has already been served this cell. The archived read then replayed the
        whole log, which is the per-caller cursor contract broken by the one event that
        moves the cursor out from under it. The rotation kept the sidecar; it just moved it,
        so the archive is asked for it under the same name.

        Consulted only for an IMPLICIT request (`since=-1` — "resume after what I was last
        served") that has no live sidecar: an explicit `since=` is the caller naming the
        byte it wants and the archive has nothing to add to it. Explicitness has to be
        carried in rather than inferred from the offset, because `since=0` — a fresh
        reader, a retry after a lost render — is a real request for the whole log that a
        truthiness test read as "no cursor given" and answered with the previous epoch's
        tail, leaving the caller no way to ask for the rest.

        Ambiguity recorded rather than fixed: once the new epoch hands out this cell id
        again, the live and archived cursors share a key. The live path wins by design.
        """
        if not implicit or offset or offset_path(self.key, cell_id).exists():
            return offset
        try:
            return int((d / "offsets" / offset_name(cell_id)).read_text())
        except (OSError, ValueError):
            return offset

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
        implicit = since < 0
        offset = default_offset(self.key, cell_id) if implicit else since
        deadline = time.monotonic() + timeout_s
        while True:
            rec = read_record(self.key, cell_id)
            if rec is not None:
                text, new_off = read_output_since(self.key, cell_id, offset)
                arch = self._rotated_under_the_record(cell_id, offset, text,
                                                      implicit=implicit)
                if arch is not None:
                    return arch
                save_offset(self.key, cell_id, new_off)
                return Completed(cell_id, rec, text)
            if not (kernel_dir(self.key) / "cells" / f"{cell_id}.log").exists():
                arch = self._archived(cell_id, offset, implicit=implicit)
                if arch is not None:
                    return arch
                # Nothing under this key ever ran this cell: no record, no log, no
                # archive. A live kernel used to make the loop report Running after every
                # timeout, forever — but PTC never queues a cell, and a confirmed one has
                # its log before any caller can learn its number, so this can only be a
                # mistyped id or one from another session and no later moment makes it
                # valid. The one exception is a cell the kernel acknowledged and has not
                # started yet; the marker names that one, and it is genuinely pending.
                #
                # `kernel_alive` and not `_kernel_known_dead` on purpose, and it is the one
                # place in this file where that is so: NotFound is as terminal for the
                # caller as KernelDied, and it is claimed only on POSITIVE evidence of a
                # live kernel. An unreadable identity collapses to "not alive" here, which
                # declines to make the claim and keeps polling — the conservative
                # direction, the same one the branch below now takes deliberately.
                if kernel_alive(self.key) and not self._pending_names(cell_id):
                    return NotFound(cell_id)
            if self._kernel_known_dead():
                return self._settle_dead(cell_id, offset, implicit=implicit)
            if time.monotonic() >= deadline:
                text, new_off = read_output_since(self.key, cell_id, offset)
                save_offset(self.key, cell_id, new_off)
                return Running(cell_id, text, new_off)
            time.sleep(0.2)

    def interrupt(self) -> None:
        """interrupt_request on the control channel, then SIGINT after a 2 s grace.

        The fallback signals the kernel this call ENTERED on, or nothing at all. It used to
        re-read the owner fresh and signal whatever pid stood there: a restart landing
        inside the grace redirects the SIGINT onto the REPLACEMENT — quite possibly
        part-way into a new cell — for a cell it never ran, and the cell this call meant to
        stop died with its kernel anyway. So the whole incarnation is captured up front
        (`Owner` compares pid, birth identity, epoch, nonce and spawn stamp, and each is
        written once per kernel) and the fallback fires only when the record still names
        it AND the pid's identity confirms it. Unknown is not a target either: an
        unreadable identity is how a recycled pid looks, and that process is somebody
        else's to interrupt.
        """
        entry = read_owner(self.key)
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
        if o is not None and o == entry and start_time_matches(o.pid,
                                                              o.proc_start_time) is True:
            try:
                os.kill(o.pid, signal.SIGINT)
            except OSError:
                pass

    def _exec_raw(self, code: str, timeout_s: float) -> Completed | Running:
        """Bootstrap-only path: no submit lock, no current.json wait (the hooks are
        installed by the very cell this runs). Follows the shell execute_reply.

        Synchronized before it sends, and here that is not belt-and-braces: it is the only
        protection this path has. Every other submission survives a missed `execute_input`
        because the kernel-side pre_run_cell publishes the same number to current.json, but
        THAT HOOK IS INSTALLED BY THIS CELL — during bootstrap the file cannot advance, so
        `_await_cell_id`'s disk witness is structurally dead and the racy SUB channel is the
        only one that can name the cell. Losing that race timed the wait out and the caller
        killed a healthy fresh kernel. `wait_for_ready` is a kernel_info round trip, which
        needs no hooks; failing it is an ordinary spawn failure (`run_bootstrap`'s caller
        tears the kernel down), and nothing has been sent when it does.
        """
        kc = self._connect()
        try:
            kc.wait_for_ready(timeout=_READY_S)
            # The key lock is `ensure_kernel`'s for the whole of this call and cells/ was
            # rotated moments ago, so the baseline is the same fact it is on the submission
            # path: whatever current.json named before this request, under exclusion.
            baseline = current_cell(self.key)
            msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
            cell_id = self._await_cell_id(kc, msg_id, baseline)
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

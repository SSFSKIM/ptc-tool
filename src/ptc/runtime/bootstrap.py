"""Runs INSIDE the kernel: tee, cell hooks, terminal records, display shim, watchdog.

Invoked by the host as a bootstrap cell:  import ptc.runtime.bootstrap as _b; _b.install('<json>')
"""
import base64
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from ptc.paths import private_open, private_write_text, secure_dir

from .state import STATE, cells

_MAX_REPR = 4096
_MAX_IMAGES = 2
_MAX_IMAGE_BYTES = 1_500_000


class _Tee:
    """Wraps the ipykernel OutStream; mirrors writes into the current cell's log."""

    def __init__(self, inner):
        self._inner = inner
        self._file = None

    def _switch(self, path: Path | None):
        if self._file:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if path is not None:
            self._file = private_open(path, "a", errors="replace", encoding="utf-8")

    def write(self, s):
        # Bind the handle once: a user background thread can be inside write() while
        # _switch(None) closes and clears it, and a write to a closed file raises
        # ValueError, not OSError (M3). Never let a mirror failure break the stream.
        f = self._file
        if f:
            try:
                f.write(s)
                f.flush()
            except (OSError, ValueError):
                pass
        return self._inner.write(s)

    def writelines(self, lines):
        # Route through write() or the log misses everything written this way (M7).
        for line in lines:
            self.write(line)

    def flush(self):
        return self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


_tees: list[_Tee] = []


def _write_json_atomic(path: Path, obj) -> None:
    private_write_text(path, json.dumps(obj), tmp=path.with_suffix(path.suffix + ".tmp"))


def _cell_no(ip, info) -> int:
    """IPython increments execution_count BEFORE pre_run_cell fires when history is
    stored, so the starting cell's number is count-1 in the store_history case (plan
    review F1). test_cell_id_alignment is the guard: if an IPython release changes
    this ordering, that test fails loudly — flip the correction here with it."""
    if getattr(info, "store_history", True):
        return int(ip.execution_count) - 1
    return int(ip.execution_count)


def _pre_run_cell(info):
    ip = _ip()
    n = _cell_no(ip, info)
    STATE.current_cell = n
    STATE.cell_started = time.perf_counter()
    STATE.last_activity = time.time()
    STATE.cell_images = []
    STATE.cell_mutations = []
    secure_dir(cells())
    for t in _tees:
        t._switch(cells() / f"{n}.log")
    _write_json_atomic(cells() / "current.json", {"cell_id": n, "started_at": time.time()})


def _post_run_cell(result):
    n = getattr(result, "execution_count", None) or STATE.current_cell
    dur = int((time.perf_counter() - STATE.cell_started) * 1000)
    err = result.error_in_exec or result.error_before_exec
    if err is None:
        status, error = "ok", None
    elif isinstance(err, KeyboardInterrupt):
        status, error = "interrupted", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": ""}
    else:
        status = "error"
        error = {"ename": type(err).__name__, "evalue": str(err)[:2000],
                 "traceback": "".join(traceback.format_exception(err))[-8000:]}
    rr = None
    if getattr(result, "result", None) is not None:
        rr = repr(result.result)[:_MAX_REPR]
    record = {"status": status, "duration_ms": dur, "result_repr": rr, "error": error,
              "images": list(STATE.cell_images), "mutations": list(STATE.cell_mutations)}
    _write_json_atomic(cells() / f"{n}.json", record)
    STATE.last_activity = time.time()
    for t in _tees:
        t._switch(None)


def _ip():
    from IPython import get_ipython
    return get_ipython()


def _install_display_shim():
    """Save published PNG/JPEG display data to cells/<n>-<k>.png and record paths."""
    ip = _ip()
    pub = ip.display_pub
    orig = pub.publish

    def publish(data=None, metadata=None, **kw):
        try:
            if data and STATE.current_cell is not None and len(STATE.cell_images) < _MAX_IMAGES:
                for mime, ext in (("image/png", "png"), ("image/jpeg", "jpg")):
                    if mime in data:
                        raw = base64.b64decode(data[mime]) if isinstance(data[mime], str) else data[mime]
                        if len(raw) <= _MAX_IMAGE_BYTES:
                            k = len(STATE.cell_images)
                            p = cells() / f"{STATE.current_cell}-{k}.{ext}"
                            with private_open(p, "wb") as f:
                                f.write(raw)
                            STATE.cell_images.append(str(p))
                        break
        except Exception:
            pass
        return orig(data=data, metadata=metadata, **kw)

    pub.publish = publish


def _log_write(text: str) -> None:
    """Append to the current cell's log only — no echo to the client stream."""
    if STATE.current_cell is None:
        return
    try:
        with private_open(cells() / f"{STATE.current_cell}.log", "a",
                          errors="replace", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _install_traceback_mirror():
    """ZMQInteractiveShell._showtraceback publishes the traceback on the iopub `error`
    channel instead of printing it, so the tee never sees it and the cell log ends up
    empty for a failing cell. A terminal IPython prints it to stdout and the log is the
    terminal-faithful transcript the caller reads back, so mirror it there. The tee
    flushes every write, so this lands in stream order."""
    ip = _ip()
    orig = ip._showtraceback

    def _showtraceback(etype, evalue, stb):
        try:
            _log_write(ip.InteractiveTB.stb2text(stb) + "\n")
        except Exception:
            pass
        return orig(etype, evalue, stb)

    ip._showtraceback = _showtraceback


def _in_flight() -> bool:
    """True from a cell's pre_run_cell until its record lands on disk. The record is
    the durable end-of-cell marker, so this stays true for a cell whose thread is
    wedged and cannot update `last_activity`."""
    n = STATE.current_cell
    return n is not None and not (cells() / f"{n}.json").exists()


def _is_idle(ttl: float) -> bool:
    """Idle is BOTH halves: nothing running AND nothing happening for `ttl` seconds.
    `last_activity` is stamped when a cell STARTS, so without the in-flight half the
    watchdog kills any cell that runs longer than the TTL, mid-execution (I1)."""
    return not _in_flight() and time.time() - STATE.last_activity > ttl


def _reap_and_exit() -> None:
    """Die, taking the kernel's spawned children with us.

    `os._exit` skips `atexit` by design, so the SDK's own child reaper never runs on
    this path and every open agent CLI would outlive the kernel as an orphan. The
    kernel is a process-group leader (spawned `start_new_session=True`) and those CLIs
    are spawned into its group, so killing the group reaps them and us in one atomic
    step — which is also the exit, and it happens while the flock is still held (F5).
    Guarded on leadership: a kernel that did not start its own group (an in-process
    test, a hand-launched ipykernel) would otherwise kill its parent's processes too.

    Background `bash()` children are the exception the group kill cannot cover: each runs
    in a session of its own, so they are reaped first, from the registry the shell keeps
    (F4). That reap never raises — nothing may come between an expired kernel and its
    exit, which happens while the flock is still held.
    """
    try:
        from ptc import bgroups
        bgroups.reap(STATE.kernel_dir)
    except Exception:
        pass
    try:
        if os.getpgid(0) == os.getpid():
            os.killpg(os.getpgid(0), signal.SIGKILL)     # never returns
    except OSError:
        pass
    os._exit(0)


def _watchdog():
    from ptc.lock import key_lock
    hours = float(STATE.config.get("idle_hours", 24.0))
    ttl = hours * 3600
    while True:
        time.sleep(min(30.0, max(ttl / 10, 0.5)))
        if not _is_idle(ttl):
            continue
        idle = time.time() - STATE.last_activity
        try:
            with key_lock(STATE.key):
                if not _is_idle(ttl):
                    continue
                (STATE.kernel_dir / "expired.marker").write_text(
                    f"expired after {idle / 3600:.2f} h idle at {time.strftime('%F %T')}")
                (STATE.kernel_dir / "owner.json").unlink(missing_ok=True)
                (STATE.kernel_dir / "ready").unlink(missing_ok=True)
                # Exit WHILE holding the flock: process death releases it atomically,
                # so a concurrent spawner can never observe a half-dead kernel (F5).
                _reap_and_exit()
        except Exception:
            continue  # cleanup failed: keep ownership and retry next tick — never
                      # exit leaving partial state behind (F5)


def install(config_json: str) -> str:
    cfg = json.loads(config_json)
    STATE.key = cfg["key"]
    STATE.kernel_dir = Path(cfg["kernel_dir"])
    STATE.config = cfg
    # post_run_cell records this very cell, but pre_run_cell was not registered when it
    # started; without a stamp its duration_ms would be perf_counter() since boot.
    STATE.cell_started = time.perf_counter()
    os.environ.setdefault("NO_COLOR", "1")
    ip = _ip()
    try:
        ip.colors = "nocolor"
    except Exception:
        pass
    for stream_name in ("stdout", "stderr"):
        tee = _Tee(getattr(sys, stream_name))
        _tees.append(tee)
        setattr(sys, stream_name, tee)
    # Monotonic cell ids across kernel epochs (F3): continue numbering above the
    # highest archived cell id so a yielded pre-restart cell id can never collide
    # with a new epoch's cell. The shell counter is pre-incremented relative to the
    # kernel counter (see _cell_no); test_cell_id_alignment guards the arithmetic.
    try:
        prev_max = 0
        for d in STATE.kernel_dir.glob("cells-prev-*"):
            for f in d.glob("*.log"):
                try:
                    prev_max = max(prev_max, int(f.stem))
                except ValueError:
                    pass
        if prev_max and prev_max + 1 > int(ip.execution_count):
            ip.execution_count = prev_max + 1              # next cell number = prev_max+1
            if getattr(ip, "kernel", None) is not None:
                ip.kernel.execution_count = prev_max       # next execute_input = prev_max+1
    except Exception:
        pass
    ip.events.register("pre_run_cell", _pre_run_cell)
    ip.events.register("post_run_cell", _post_run_cell)
    _install_display_shim()
    _install_traceback_mirror()
    threading.Thread(target=_watchdog, daemon=True, name="ptc-watchdog").start()
    from . import bind
    bind(ip)
    return "ptc-bootstrap-ok"

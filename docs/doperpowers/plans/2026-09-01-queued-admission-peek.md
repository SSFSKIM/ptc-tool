# Queued Admission + Peek Channel (v0.3 initiative 2) Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use doperpowers:subagent-driven-execution to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `exec(queue=True)` waits for the kernel's slot instead of returning busy, and a new `peek` channel reads variable reprs while a cell is running.

**Architecture:** Queueing is wait-then-submit — a poll loop in FRONT of the untouched F2 admission machinery (`exec_cell`), sharing the one `timeout_s` budget and returning an honest `queue-timeout` Busy on exhaustion. Peek is a kernel-side daemon thread serving `kernel_dir/peek.sock` (one JSON line in, one out), AST-restricted to Name/Attribute/constant-Subscript chains, evaluated on a per-request thread joined at 5 s; the adapter/CLI side is a small unix-socket client. Both are additive: no protocol bump (a pre-peek kernel simply has no socket, and the tool says so).

**Tech Stack:** Python 3.12 stdlib (ast, socket, threading), pytest; no new dependencies.

**Spec:** `docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md`, section `## Structural follow-on (v0.3 line)` → "Initiative 2 — queued admission and the peek channel", plus its Decision Log entries dated 2026-09-01 (wait-then-submit choice; peek mutation-resistant-not-read-only). Conflicts during execution resolve against the spec.

## Global Constraints

- The F2 admission machinery in `src/ptc/client.py` (submit lock, pending marker, epoch/nonce discharge) is NOT modified — the queue loop sits strictly in front of `exec_cell`.
- Exact strings tests pin: Busy reason `"queue-timeout"`; the plain-busy hint `pass queue=True to wait for the slot`; the pre-peek message `kernel build {build} predates peek — restart() to upgrade` (build may be `?` when unrecorded); peek refusal names the offending node type.
- Peek bounds: repr cap **4000** chars; eval-thread join **5.0 s**; per-connection socket read timeout **2.0 s**; max request **65536** bytes; socket `peek.sock` chmod **0600** inside the 0700 kernel dir.
- `Busy` gains `queued_s: float | None = None` as a DEFAULTED LAST field — every existing construction stays valid.
- The hooks PreToolUse matcher becomes `mcp__(plugin_ptc_)?ptc__(exec|wait|interrupt|restart|kernels|peek)` — subagent auto-keying must cover peek.
- Peek's failure is never bootstrap's failure: `install_peek` swallows OSError and returns; the daemon thread is `daemon=True`.
- No version bumps, no push (controller ships after the final review).
- Run tests with `uv run pytest …` from the repo root.

**Orientation (read once):** admission today: `KernelClient.exec_cell` returns `Busy(cell_id, reason)` with reasons `running` / `pending-unconfirmed` / `lock-held`; `src/ptc/shape.py::render` maps them through `_BUSY_TEXT`/`_BUSY_TEXT_NO_ID` and ends the busy render with "…interrupt() to stop it, or resubmit after it finishes. Nothing was queued." The MCP handlers live in `src/ptc/mcp.py` (plain async functions registered at the bottom via `server.tool(name=…, structured_output=False)(fn)` in a tuple loop); the CLI parser is `src/ptc/cli.py::_run` (subcommands built by a local `com()` helper). Kernel-side bootstrap is `src/ptc/runtime/bootstrap.py::install` (it already starts watchdog threads and captures `ip = _ip()`; `ip.user_ns` is the live namespace). `test/conftest.py`'s `ptc_home` fixture provides real kernels for integration tests.

---

### Task 1: Spike S8 — the peek daemon under a busy kernel

**Files:**
- Create: `test/spikes/s8_peek_daemon.py`
- Modify (verdict only): `docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md` (S8 spike entry)

The spec declares S8 as a plan-stage spike gating the daemon design. The deliverable is knowledge, not shipped code — build → run → record, no TDD.

**Question:** does a daemon thread started at bootstrap answer a unix-socket request inside 1 s while the kernel's main thread runs a tight loop, and does a stale socket after kernel death confuse nothing?

**Promote/discard criteria (spec, verbatim):** "daemon-thread responsiveness under a busy kernel (tight-loop cell) — peek must answer inside 1 s; also verify a stale socket after kernel death confuses nothing (discovery reports the kernel dead before anyone consults the socket)."

- [ ] **Step 1: Write the probe**

Create `test/spikes/s8_peek_daemon.py`:

```python
"""S8: a daemon thread serving a unix socket answers while the main thread spins.

Run:  uv run python test/spikes/s8_peek_daemon.py
It provisions nothing: it spawns a REAL kernel in a temp PTC_HOME (using the cached
test venv via the same mechanics as test/conftest.py), injects a prototype daemon by
exec'ing a cell, drives the kernel busy, and times a peek round trip.
"""
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent.parent

def main():
    cache = PKG / ".venv-kernel"
    assert (cache / "bin" / "python").exists(), \
        "cached test venv missing — run `uv run pytest test/unit/test_paths.py -q` once first"
    tmp = Path(tempfile.mkdtemp(prefix="s8-"))
    home = tmp / "home"; home.mkdir()
    (home / "venv").symlink_to(cache.resolve())
    os.environ["PTC_HOME"] = str(home)
    from ptc.kernel import ensure_kernel, kill_kernel
    from ptc.client import KernelClient
    from ptc.paths import Config, kernel_dir
    cfg = Config.from_env()
    ensure_kernel("s8", config=cfg)
    kc = KernelClient("s8")
    # prototype daemon, injected as a cell — the real one will live in bootstrap
    daemon_src = r'''
import json, socket, threading, os
def _serve(sock, ns):
    while True:
        c, _ = sock.accept()
        data = c.recv(65536)
        expr = json.loads(data)["expr"]
        try:
            c.sendall((json.dumps({"repr": repr(eval(expr, {"__builtins__": {}}, ns))}) + "\n").encode())
        except Exception as e:
            c.sendall((json.dumps({"error": str(e)}) + "\n").encode())
        c.close()
_p = %r
_s = socket.socket(socket.AF_UNIX); _s.bind(_p); os.chmod(_p, 0o600); _s.listen(4)
threading.Thread(target=_serve, args=(_s, globals()), daemon=True).start()
print("daemon up")
''' % str(kernel_dir("s8") / "peek.sock")
    out = kc.exec_cell(daemon_src, timeout_s=30, config=cfg)
    assert "daemon up" in out.output, out
    kc.exec_cell("n = 0", timeout_s=30, config=cfg)
    # drive the kernel busy: a tight loop, submitted and left running
    r = kc.exec_cell("while True:\n    n += 1", timeout_s=0.5, config=cfg)
    print("busy cell state:", type(r).__name__)
    # peek while busy, timed
    t0 = time.monotonic()
    s = socket.socket(socket.AF_UNIX); s.settimeout(3.0)
    s.connect(str(kernel_dir("s8") / "peek.sock"))
    s.sendall(json.dumps({"expr": "n"}).encode())
    reply = s.recv(65536); s.close()
    dt = time.monotonic() - t0
    print(f"peek while busy: {reply!r} in {dt*1000:.0f} ms")
    assert dt < 1.0, f"answer took {dt:.2f}s"
    assert json.loads(reply)["repr"].isdigit()
    kc.interrupt()
    kill_kernel("s8")
    # stale socket after death: connect must fail cleanly, and kernel_alive says dead first
    from ptc.kernel import kernel_alive
    assert not kernel_alive("s8")
    try:
        s2 = socket.socket(socket.AF_UNIX); s2.settimeout(1.0)
        s2.connect(str(kernel_dir("s8") / "peek.sock"))
        print("stale socket: connect unexpectedly succeeded (kernel gone — refused expected)")
    except OSError as e:
        print(f"stale socket refused cleanly: {type(e).__name__}")
    print("S8 VERDICT INPUTS COMPLETE")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `uv run python test/spikes/s8_peek_daemon.py`
Observe and record: the busy-cell state name, the peek latency line, the stale-socket line, and the final `S8 VERDICT INPUTS COMPLETE`.

- [ ] **Step 3: Record the verdict in the spec**

Append to the S8 entry in `docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md` (the `- **S8 — peek daemon under a busy kernel.**` bullet), in the established `*Verdict (…): PROMOTE/…*` form: the measured latency, the busy mechanism observed (GIL sharing between the tight loop and the daemon thread), and the stale-socket behavior. Apply the criteria: PROMOTE (Tasks 2-3 harden it) if latency < 1 s and the stale socket confused nothing; otherwise record what failed and STOP — report BLOCKED to the controller (the fallback would be a design change, not yours to make).

- [ ] **Step 4: Commit**

```bash
git add test/spikes/s8_peek_daemon.py docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md
git commit -m "f5(ptc): S8 spike — peek daemon answers under a busy kernel (verdict recorded)"
```

---

### Task 2: The peek runtime — server daemon + client reader

**Files:**
- Create: `src/ptc/runtime/peek.py` (kernel-side daemon)
- Create: `src/ptc/peek_client.py` (adapter/CLI-side reader)
- Modify: `src/ptc/runtime/bootstrap.py` (start the daemon in `install()`)
- Test: `test/unit/test_peek.py` (new), `test/integration/test_peek_live.py` (new)

**Interfaces:**
- Consumes: from Task 1 the promoted mechanism; `STATE.kernel_dir` and `_ip().user_ns` in bootstrap.
- Produces (Task 3 relies on): `peek_client.peek_kernel(key: str, expr: str, timeout_s: float = 6.0) -> dict` returning `{"repr": str, "truncated": bool}` or `{"error": str}`, raising `peek_client.PeekUnavailable` when no socket exists or nothing answers; `runtime.peek.install_peek(kernel_dir: Path, namespace) -> None`; `runtime.peek._validate(expr: str) -> ast.Expression` (raises ValueError naming the refused node); `runtime.peek.REPR_CAP == 4000`.

- [ ] **Step 1: Write the failing unit tests**

Create `test/unit/test_peek.py`:

```python
"""Peek: AST restriction, bounds, and the full socket loop against a plain dict —
no kernel needed (the daemon serves whatever namespace mapping it is handed)."""
import json
import socket
import threading
import time

import pytest
from ptc import peek_client
from ptc.runtime import peek


@pytest.mark.parametrize("expr", ["x", "x.y", "x.y.z", "d['k']", "xs[0]", "m[('a', 1)]"])
def test_validate_allows_chains(expr):
    peek._validate(expr)


@pytest.mark.parametrize("expr,node", [
    ("f()", "Call"), ("os.system('x')", "Call"), ("x + 1", "BinOp"),
    ("[y for y in xs]", "ListComp"), ("xs[i]", "Name"),   # non-constant subscript index
    ("(lambda: 1)()", "Call"),
])
def test_validate_refuses_everything_else(expr, node):
    with pytest.raises(ValueError, match=node):
        peek._validate(expr)


def test_validate_refuses_statements():
    with pytest.raises(SyntaxError):
        peek._validate("x = 1")


def _daemon(tmp_path, namespace):
    peek.install_peek(tmp_path, namespace)
    return tmp_path / "peek.sock"


def test_round_trip_repr_and_cap(tmp_path):
    ns = {"x": {"k": "v" * 5000}, "small": 42}
    sock_path = _daemon(tmp_path, ns)
    r = peek_client.peek_kernel_path(sock_path, "small")
    assert r == {"repr": "42", "truncated": False}
    r = peek_client.peek_kernel_path(sock_path, "x")
    assert r["truncated"] is True and len(r["repr"]) == peek.REPR_CAP


def test_error_paths_travel_as_error_replies(tmp_path):
    sock_path = _daemon(tmp_path, {"x": 1})
    assert "NameError" in peek_client.peek_kernel_path(sock_path, "missing")["error"]
    assert "Call" in peek_client.peek_kernel_path(sock_path, "f()")["error"]


def test_blocking_repr_times_out_and_channel_survives(tmp_path):
    class Stuck:
        def __repr__(self):
            time.sleep(60)
            return "never"
    ns = {"stuck": Stuck(), "ok": 1}
    sock_path = _daemon(tmp_path, ns)
    t0 = time.monotonic()
    r = peek_client.peek_kernel_path(sock_path, "stuck", timeout_s=8.0)
    assert r == {"error": "evaluation timed out"}
    assert time.monotonic() - t0 < 7.0          # the 5 s join, not the socket timeout
    # a subsequent peek still answers (acceptance 5's unit twin)
    assert peek_client.peek_kernel_path(sock_path, "ok")["repr"] == "1"


def test_peek_unavailable_when_no_socket(tmp_path):
    with pytest.raises(peek_client.PeekUnavailable):
        peek_client.peek_kernel_path(tmp_path / "peek.sock", "x")


def test_mutation_is_inexpressible_but_getattr_runs_user_code(tmp_path):
    """The honest claim (spec): mutation-RESISTANT, not read-only — property access runs
    user code; what the grammar refuses is calls and assignments."""
    class Trap:
        @property
        def boom(self):
            return "property ran"
    sock_path = _daemon(tmp_path, {"t": Trap()})
    assert peek_client.peek_kernel_path(sock_path, "t.boom")["repr"] == "'property ran'"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_peek.py -v`
Expected: collection ERROR (`ModuleNotFoundError: No module named 'ptc.runtime.peek'`).

- [ ] **Step 3: Implement the server half**

Create `src/ptc/runtime/peek.py`:

```python
"""Namespace inspection over a unix socket, served while cells run.

Mutation-RESISTANT, not read-only and not a boundary (spec, initiative 2): the grammar
makes calls and assignments inexpressible, but attribute access runs properties and
descriptors, subscript runs __getitem__, and repr runs __repr__ — all user code, inside
the kernel's existing trust domain. Evaluation runs on a per-request thread joined at
5 s so a blocking __repr__ costs one abandoned daemon thread, never the channel.
"""
import ast
import json
import os
import socket
import threading

REPR_CAP = 4000
_EVAL_JOIN_S = 5.0
_READ_TIMEOUT_S = 2.0
_MAX_REQUEST = 65536

#: Load/Expression/Tuple ride along as structure; the shape that matters is what is
#: ABSENT: Call, BinOp, comprehension, assignment — anything that computes rather than
#: navigates. A Tuple is admitted only as a subscript key of constants (m[('a', 1)]),
#: which ast.walk visits as Tuple-of-Constant.
_ALLOWED = (ast.Expression, ast.Name, ast.Attribute, ast.Subscript, ast.Constant,
            ast.Tuple, ast.Load)


def _validate(expr: str) -> ast.Expression:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(
                "peek allows Name/Attribute/constant-Subscript chains only "
                f"(refused: {type(node).__name__})")
        if isinstance(node, ast.Subscript) and not isinstance(
                node.slice, (ast.Constant, ast.Tuple)):
            raise ValueError(
                "peek allows Name/Attribute/constant-Subscript chains only "
                f"(refused: {type(node.slice).__name__} as a subscript index)")
        if isinstance(node, ast.Tuple) and not all(
                isinstance(e, ast.Constant) for e in node.elts):
            raise ValueError(
                "peek allows Name/Attribute/constant-Subscript chains only "
                "(refused: Tuple with non-constant elements)")
    return tree


def _evaluate(tree: ast.Expression, namespace) -> dict:
    code = compile(tree, "<peek>", "eval")
    box: dict = {}

    def run():
        try:
            # empty builtins: a navigation chain needs none, and their absence is one
            # more thing the grammar's refusals do not have to carry alone
            box["repr"] = repr(eval(code, {"__builtins__": {}}, namespace))
        except BaseException as e:  # noqa: BLE001 — the reply channel carries it
            box["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(_EVAL_JOIN_S)
    if t.is_alive():
        return {"error": "evaluation timed out"}
    if "error" in box:
        return {"error": box["error"]}
    r = box["repr"]
    return {"repr": r[:REPR_CAP], "truncated": len(r) > REPR_CAP}


def _serve(sock: socket.socket, namespace) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return                      # socket closed: kernel is going down
        try:
            conn.settimeout(_READ_TIMEOUT_S)
            data = b""
            while b"\n" not in data and len(data) < _MAX_REQUEST:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            try:
                req = json.loads(data.decode("utf-8", "replace"))
                reply = _evaluate(_validate(str(req["expr"])), namespace)
            except Exception as e:      # noqa: BLE001 — one bad request, one error reply
                reply = {"error": f"{type(e).__name__}: {e}"}
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass                        # a hung or vanished client costs nothing
        finally:
            try:
                conn.close()
            except OSError:
                pass


def install_peek(kernel_dir, namespace) -> None:
    """Serve `kernel_dir/peek.sock`. Failure is swallowed: peek is a convenience and
    must never fail bootstrap."""
    path = kernel_dir / "peek.sock"
    try:
        path.unlink(missing_ok=True)    # a previous incarnation's stale socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen(4)
    except OSError:
        return
    threading.Thread(target=_serve, args=(sock, namespace), daemon=True,
                     name="ptc-peek").start()
```

- [ ] **Step 4: Implement the client half**

Create `src/ptc/peek_client.py`:

```python
"""Adapter/CLI-side reader for a kernel's peek socket."""
import json
import socket

from .paths import kernel_dir


class PeekUnavailable(RuntimeError):
    """No socket to ask: the kernel predates peek, or nothing is answering."""


def peek_kernel_path(sock_path, expr: str, timeout_s: float = 6.0) -> dict:
    if not sock_path.exists():
        raise PeekUnavailable(f"no peek socket at {sock_path}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        try:
            s.connect(str(sock_path))
            s.sendall((json.dumps({"expr": expr}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError as e:
            raise PeekUnavailable(f"peek socket not answering: {e}") from e
    finally:
        s.close()
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise PeekUnavailable(f"peek socket answered garbage: {e}") from e


def peek_kernel(key: str, expr: str, timeout_s: float = 6.0) -> dict:
    """The keyed form the MCP tool and CLI use; `timeout_s` bounds the whole round trip
    (the server joins its eval thread at 5 s, so 6 covers it)."""
    return peek_kernel_path(kernel_dir(key) / "peek.sock", expr, timeout_s)
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `uv run pytest test/unit/test_peek.py -v`
Expected: PASS (12 tests).

- [ ] **Step 6: Wire the daemon into bootstrap**

In `src/ptc/runtime/bootstrap.py::install`, after the display shim / hook registrations
(beside the other `try/except`-belted installs — find `_install_display_shim()` and place
this adjacently), add:

```python
    try:
        from . import peek as _peek
        _peek.install_peek(STATE.kernel_dir, ip.user_ns)
    except Exception:
        pass   # peek is a convenience: its absence must never fail bootstrap
```

- [ ] **Step 7: Write the live integration test**

Create `test/integration/test_peek_live.py`:

```python
"""Peek against a REAL busy kernel — the spike's mechanism, hardened (spec acceptance
1/5 twins). Follows test_kernel_lifecycle.py's spawn idioms."""
import time

from ptc import peek_client
from ptc.client import KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_peek_answers_under_a_busy_kernel_within_a_second(ptc_home):
    cfg = Config.from_env()
    ensure_kernel("peeklive", cwd=str(ptc_home), config=cfg)
    kc = KernelClient("peeklive")
    kc.exec_cell("n = 0", timeout_s=30, config=cfg)
    kc.exec_cell("while True:\n    n += 1", timeout_s=0.5, config=cfg)  # leaves it running
    try:
        t0 = time.monotonic()
        r = peek_client.peek_kernel("peeklive", "n")
        assert (time.monotonic() - t0) < 1.0
        assert r["repr"].isdigit() and r["truncated"] is False
    finally:
        kc.interrupt()
        kill_kernel("peeklive")


def test_peek_on_a_dead_kernel_raises_unavailable(ptc_home):
    cfg = Config.from_env()
    ensure_kernel("peekdead", cwd=str(ptc_home), config=cfg)
    kill_kernel("peekdead")
    import pytest
    with pytest.raises(peek_client.PeekUnavailable):
        peek_client.peek_kernel("peekdead", "x")
```

- [ ] **Step 8: Run integration + full unit file**

Run: `uv run pytest test/integration/test_peek_live.py test/unit/test_peek.py -v`
Expected: PASS. (If the dead-kernel case finds the stale socket CONNECTABLE rather than
absent — a unix socket file survives its process — the client's connect gets
ECONNREFUSED, which `peek_kernel_path` wraps as PeekUnavailable; the test passes either
way by construction. If it does not, report the observed behavior as a concern.)

- [ ] **Step 9: Commit**

```bash
git add src/ptc/runtime/peek.py src/ptc/peek_client.py src/ptc/runtime/bootstrap.py test/unit/test_peek.py test/integration/test_peek_live.py
git commit -m "f5(ptc): peek runtime — AST-restricted namespace reads over a kernel socket, served while busy (v0.3 i2)"
```

---

### Task 3: Peek surfaces — MCP tool, CLI verb, hooks matcher, doctrine

**Files:**
- Modify: `src/ptc/mcp.py` (peek tool + registration + instructions line)
- Modify: `src/ptc/cli.py` (peek subcommand)
- Modify: `hooks/hooks.json` (matcher gains `peek`)
- Modify: `skills/ptc/SKILL.md` (peek doctrine, one bullet)
- Test: `test/unit/test_mcp_descriptions.py`, `test/unit/test_hook_script.py` and/or `test/unit/test_plugin_packaging.py` (wherever the matcher regex is pinned — grep `kernels\)` to find it), `test/unit/test_cli_commands.py`, `test/integration/test_subagent_keying.py` (subagent peek case)

**Interfaces:**
- Consumes: from Task 2: `peek_client.peek_kernel(key, expr) -> dict` + `PeekUnavailable`.
- Produces: MCP tool `peek(expr, session=)`; CLI `ptc peek <expr>`; matcher `mcp__(plugin_ptc_)?ptc__(exec|wait|interrupt|restart|kernels|peek)`.

- [ ] **Step 1: Write the failing tests**

In `test/unit/test_mcp_descriptions.py`, append (match the file's existing style of
listing tools/descriptions):

```python
def test_peek_description_carries_the_contract():
    from ptc import mcp
    doc = mcp.peek_tool.__doc__
    for token in ("busy", "repr", "restart()", "no calls"):
        assert token in doc, token


def test_instructions_mention_peek_and_queue():
    from ptc.mcp import INSTRUCTIONS
    assert "peek" in INSTRUCTIONS and "queue=True" in INSTRUCTIONS
```

(Both tokens go green in THIS task: Step 3 adds the peek+queue sentence to INSTRUCTIONS —
the `queue=` parameter itself lands in Task 4, but the digest line is doctrine and ships
with the peek surfaces.)

Where the matcher regex is pinned (grep for `restart|kernels`), extend the expected
regex to include `peek`. In `test/unit/test_cli_commands.py`, add a peek test following
the file's fake/monkeypatch idiom:

```python
def test_cli_peek_prints_repr(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setenv("PTC_SESSION", "cli-peek")
    import ptc.cli as cli
    monkeypatch.setattr(cli, "peek_kernel", lambda key, expr: {"repr": "42", "truncated": False})
    assert cli.main(["peek", "n"]) == 0
    assert "42" in capsys.readouterr().out


def test_cli_peek_says_predates_on_unavailable(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setenv("PTC_SESSION", "cli-peek2")
    import ptc.cli as cli
    from ptc.peek_client import PeekUnavailable
    def boom(key, expr):
        raise PeekUnavailable("no socket")
    monkeypatch.setattr(cli, "peek_kernel", boom)
    assert cli.main(["peek", "n"]) == 1
    assert "predates peek" in capsys.readouterr().err
```

In `test/integration/test_subagent_keying.py`, add a case following the file's existing
dispatch-path pattern (it drives `MCPServer.call_tool` with a `_meta` dict and a hook
mapping file): parent kernel sets `TOKEN = 'parent-secret'`; write a tooluse mapping for
a fake subagent; call the `peek` tool with `expr="TOKEN"` and that `_meta`; assert the
reply does NOT contain `parent-secret` (the subagent's own kernel has no such name — a
NameError error reply is the correct outcome, and IS the isolation proof).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_mcp_descriptions.py test/unit/test_cli_commands.py -k "peek" -v`
Expected: FAIL/ERROR (no `peek_tool`, no CLI verb).

- [ ] **Step 3: Implement the MCP tool**

In `src/ptc/mcp.py`, beside the other handlers:

```python
def _peek_text(key: str, expr: str) -> str:
    from .discovery import read_meta
    from .kernel import kernel_alive
    from .peek_client import PeekUnavailable, peek_kernel
    if not kernel_alive(key):
        return f"[no live kernel for {key} — exec first]"
    try:
        reply = peek_kernel(key, expr)
    except PeekUnavailable:
        build = read_meta(key).get("build") or "?"
        return f"[kernel build {build} predates peek — restart() to upgrade]"
    if "error" in reply:
        return f"[peek {key}] error: {reply['error']}"
    note = " …[truncated at 4000 chars]" if reply.get("truncated") else ""
    return f"[peek {key}] {reply['repr']}{note}"


async def peek_tool(expr: str, session: str | None = None, ctx: Context = None) -> list:
    """Read a value while the kernel is BUSY — the one channel that works mid-cell
    (wait tails output; peek reads variables). `expr` is a Name/attribute/constant-index
    chain evaluated against the live namespace, repr capped at 4000 chars; no calls, no
    assignments — refused before evaluation. Not a boundary: attribute access and repr
    run the object's own code. A kernel from a build before peek has no channel —
    restart() upgrades it."""
    r = await asyncio.to_thread(_resolve, session, tool_use_id=_tool_use_id(ctx))
    text = await asyncio.to_thread(_peek_text, r.key, expr)
    return [TextContent(type="text", text=text)]
```

Register it: the tuple loop at the bottom gains `(peek_tool, "peek")`. Add one sentence
to `INSTRUCTIONS` (before the skill-pointer sentence): `peek(expr) reads a variable's
repr while a cell runs; exec(queue=True) waits for the slot instead of returning busy.`

- [ ] **Step 4: Implement the CLI verb**

In `src/ptc/cli.py`: import `from .peek_client import PeekUnavailable, peek_kernel` at
the top; in `_run`, add `sp = com("peek"); sp.add_argument("expr")` beside the other
subcommands, and after `key, notice, resolved = _pick_session(a.session)`:

```python
    if a.cmd == "peek":
        try:
            reply = peek_kernel(key, a.expr)
        except PeekUnavailable:
            print(f"ptc: kernel build predates peek — restart() to upgrade "
                  f"(or no kernel is running)", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps({"key": key, **reply}))
            return 0 if "error" not in reply else 1
        if "error" in reply:
            print(f"ptc: peek error: {reply['error']}", file=sys.stderr)
            return 1
        note = " …[truncated]" if reply.get("truncated") else ""
        print(reply["repr"] + note)
        return 0
```

- [ ] **Step 5: hooks matcher + SKILL.md**

`hooks/hooks.json`: the PreToolUse matcher becomes
`"mcp__(plugin_ptc_)?ptc__(exec|wait|interrupt|restart|kernels|peek)"`.

`skills/ptc/SKILL.md`: add one bullet beside the busy/async doctrine:

```
- While a cell runs, `peek` (MCP tool / `ptc peek <expr>`) reads a variable's repr from
  the live namespace — chains only, no calls; wait() tails output, peek reads values.
  Kernels from pre-peek builds have no channel; restart() upgrades them.
```

- [ ] **Step 6: Run all named test files**

Run: `uv run pytest test/unit/test_mcp_descriptions.py test/unit/test_cli_commands.py test/unit/test_hook_script.py test/unit/test_plugin_packaging.py test/integration/test_subagent_keying.py -v`
Expected: PASS (except the deliberate queue=True xfail/red if you chose the known-red route — name it).

- [ ] **Step 7: Commit**

```bash
git add src/ptc/mcp.py src/ptc/cli.py hooks/hooks.json skills/ptc/SKILL.md test/unit/test_mcp_descriptions.py test/unit/test_cli_commands.py test/unit/test_hook_script.py test/unit/test_plugin_packaging.py test/integration/test_subagent_keying.py
git commit -m "f5(ptc): peek surfaces — MCP tool, CLI verb, subagent-keyed hook matcher, doctrine (v0.3 i2)"
```

---

### Task 4: Queued admission — wait-then-submit

**Files:**
- Modify: `src/ptc/client.py` (Busy field + `exec_cell_queued`)
- Modify: `src/ptc/shape.py` (busy hint + queue-timeout render)
- Modify: `src/ptc/mcp.py` (exec `queue` param + docstring)
- Modify: `src/ptc/cli.py` (`--queue` flag)
- Test: `test/unit/test_queued_admission.py` (new), `test/unit/test_shape.py` (render pins), `test/unit/test_mcp_descriptions.py` (the queue token goes green), `test/integration/test_busy_yield_wait.py` (queued exec behind a sleeping cell)

**Interfaces:**
- Consumes: nothing beyond the existing `exec_cell` (NOT modified) and `is_busy`.
- Produces: `KernelClient.exec_cell_queued(code, timeout_s, config) -> Completed | Running | Busy`; `Busy.queued_s: float | None = None` (defaulted last); Busy reason `"queue-timeout"`.

- [ ] **Step 1: Write the failing unit tests**

Create `test/unit/test_queued_admission.py`:

```python
"""Wait-then-submit: a poll loop in FRONT of the untouched admission machinery."""
import time

from ptc.client import Busy, Completed, KernelClient
from ptc.cells import CellRecord
from ptc.paths import Config


def _completed(cid=7):
    return Completed(cid, CellRecord(status="ok", duration_ms=1, result_repr="4",
                                     error=None, images=[], mutations=[]), "4")


def test_queued_exec_lands_when_the_slot_frees(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kc = KernelClient("q1")
    calls = {"n": 0}
    def fake_exec(code, timeout_s, config):
        calls["n"] += 1
        return Busy(3, reason="running") if calls["n"] < 3 else _completed()
    monkeypatch.setattr(kc, "exec_cell", fake_exec)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = kc.exec_cell_queued("2+2", timeout_s=30, config=Config.from_env())
    assert isinstance(out, Completed) and calls["n"] == 3


def test_queue_timeout_returns_an_honest_busy(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kc = KernelClient("q2")
    monkeypatch.setattr(kc, "exec_cell",
                        lambda code, timeout_s, config: Busy(3, reason="running"))
    monkeypatch.setattr(kc, "is_busy", lambda: Busy(3, reason="running"))
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    def sleep(s):
        clock["t"] += s
    monkeypatch.setattr(time, "sleep", sleep)
    out = kc.exec_cell_queued("2+2", timeout_s=2.0, config=Config.from_env())
    assert isinstance(out, Busy) and out.reason == "queue-timeout"
    assert out.cell_id == 3
    assert out.queued_s is not None and out.queued_s >= 2.0


def test_lost_admission_race_keeps_polling(monkeypatch, tmp_path):
    """A poller that sees the slot free but loses exec_cell's own atomic admission gets
    Busy back — that is one more poll, not a return."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    kc = KernelClient("q3")
    seq = [Busy(None, reason="lock-held"), _completed()]
    monkeypatch.setattr(kc, "exec_cell", lambda code, timeout_s, config: seq.pop(0))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = kc.exec_cell_queued("2+2", timeout_s=30, config=Config.from_env())
    assert isinstance(out, Completed)
```

In `test/unit/test_shape.py`, append render pins (follow the file's existing Busy render
test style):

```python
def test_busy_render_carries_the_queue_hint():
    from ptc.client import Busy
    from ptc.paths import Config
    from ptc.shape import render
    text = render(Busy(3, reason="running"), "k", Config.from_env()).text
    assert "pass queue=True to wait for the slot" in text
    assert "Nothing was queued" in text


def test_queue_timeout_render_names_duration_and_holder():
    from ptc.client import Busy
    from ptc.paths import Config
    from ptc.shape import render
    text = render(Busy(3, reason="queue-timeout", queued_s=42.0), "k",
                  Config.from_env()).text
    assert "queued 42s" in text and "cell 3" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_queued_admission.py test/unit/test_shape.py -k "queue" -v`
Expected: FAIL (`Busy.__init__` has no `queued_s`; no `exec_cell_queued`; renders lack the strings).

- [ ] **Step 3: Implement**

`src/ptc/client.py` — `Busy` gains the defaulted field:

```python
@dataclass
class Busy:
    cell_id: int | None
    #: why admission was refused — "running", "pending-unconfirmed", "lock-held",
    #: or "queue-timeout" (a queued exec's budget ran out before the slot freed)
    reason: str = ""
    #: how long a queue=True call waited before giving up; None on ordinary Busy
    queued_s: float | None = None
```

and, after `exec_cell`:

```python
    def exec_cell_queued(self, code: str, timeout_s: float,
                         config: Config) -> Completed | Running | Busy:
        """`exec_cell` behind a wait-for-the-slot loop (spec: wait-then-submit).

        The admission machinery is untouched — this polls in FRONT of it, so every
        guarantee F2 makes still holds; the one budget covers the queue wait AND the
        submitted cell's follow (a call that queued 100 s of a 300 s budget follows for
        at most 200). A lost admission race (another poller won the slot) comes back as
        Busy and is one more poll, not a return. Exhaustion returns an honest
        `queue-timeout` Busy naming the holding cell and the time spent queued —
        nothing was ever submitted, exactly as an ordinary Busy.
        """
        import random
        start = time.monotonic()
        deadline = start + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                held = self.is_busy()
                return Busy(held.cell_id if held is not None else None,
                            reason="queue-timeout",
                            queued_s=time.monotonic() - start)
            out = self.exec_cell(code, timeout_s=remaining, config=config)
            if not isinstance(out, Busy):
                return out
            time.sleep(min(0.5 + random.random() * 0.25, max(remaining, 0.0)))
```

`src/ptc/shape.py` — extend the two maps and the closing sentence:

```python
_BUSY_TEXT = {
    "running": "cell {id} is still running",
    "pending-unconfirmed": "cell {id} was just submitted and is awaiting the kernel's confirmation",
    "lock-held": "cell {id} is busy — another submission currently holds the kernel's admission lock",
    "queue-timeout": "cell {id} is still running — this call queued for its whole budget and the slot never freed",
}
```

with `"queue-timeout": "the slot never freed within this call's budget"` added to
`_BUSY_TEXT_NO_ID`. In the Busy branch of `render`, prepend the duration when known and
extend the closing sentence:

```python
        queued = (f"queued {outcome.queued_s:.0f}s — "
                  if outcome.queued_s is not None else "")
        return Rendered(
            f"[kernel busy{' · [keying: adapter-local]' if degraded else ''}] "
            f"{queued}{which}. "
            + (f"That cell may belong to another caller of this shared kernel: its output is "
               f"that cell's, not the result of what you just tried to run — "
               f"wait(cell_id={outcome.cell_id}) tells you when the kernel frees, and "
               "collects that output only for the cell's own submitter. " if has_id else "")
            + "interrupt() to stop it, resubmit after it finishes, or pass queue=True "
              "to wait for the slot. Nothing was queued.", [])
```

`src/ptc/mcp.py` — `exec_tool` gains `queue: bool = False` (before `ctx`), the docstring
gains one sentence after the busy sentence: `queue=True waits for the slot instead
(budget = timeout_s; with a long budget the wait auto-backgrounds and your turn arrives
as a notification).`, and the call site becomes:

```python
    runner = KernelClient(r.key).exec_cell_queued if queue else KernelClient(r.key).exec_cell
    outcome = await asyncio.to_thread(runner, code, timeout_s=cfg.yield_s, config=cfg)
```

(bind `kc = KernelClient(r.key)` once instead of constructing twice — write it cleanly).

`src/ptc/cli.py` — the exec subparser gains
`sp.add_argument("--queue", action="store_true", help="wait for the kernel's slot instead of exiting busy")`,
and the exec branch routes through `exec_cell_queued` when `a.queue`.

- [ ] **Step 4: Run the named unit files**

Run: `uv run pytest test/unit/test_queued_admission.py test/unit/test_shape.py test/unit/test_mcp_descriptions.py -v`
Expected: PASS (the busy-render pins in test_shape.py's EXISTING tests may pin the old
closing sentence — update those expectations to the new sentence; they are asserting the
sentence, not the machinery).

- [ ] **Step 5: Integration — queued exec behind a real sleeping cell**

Append to `test/integration/test_busy_yield_wait.py` (follow its local idioms):

```python
def test_queued_exec_lands_behind_a_sleeping_cell(ptc_home):
    from ptc.client import Completed, KernelClient
    from ptc.kernel import ensure_kernel, kill_kernel
    from ptc.paths import Config
    cfg = Config.from_env()
    ensure_kernel("qlive", cwd=str(ptc_home), config=cfg)
    kc = KernelClient("qlive")
    kc.exec_cell("import time", timeout_s=30, config=cfg)
    kc.exec_cell("time.sleep(3)", timeout_s=0.3, config=cfg)   # leaves it running
    out = kc.exec_cell_queued("2+2", timeout_s=60, config=cfg)
    assert isinstance(out, Completed) and out.record.result_repr == "4"
    kill_kernel("qlive")
```

Run: `uv run pytest test/integration/test_busy_yield_wait.py -v` — expected PASS.

- [ ] **Step 6: Full suite, then commit**

Run: `uv run pytest test/ -q` — expected all green.

```bash
git add src/ptc/client.py src/ptc/shape.py src/ptc/mcp.py src/ptc/cli.py test/unit/test_queued_admission.py test/unit/test_shape.py test/unit/test_mcp_descriptions.py test/integration/test_busy_yield_wait.py
git commit -m "f5(ptc): exec(queue=True) — wait-then-submit in front of the untouched admission machinery (v0.3 i2)"
```

---

### Task 5: Final verification — the spec's acceptance, as written

**Files:** none (commands only; no commits).

The spec's acceptance for initiative 2 (quoted verbatim):

> 1. A cell running `while True: n += 1` holds the kernel; plain `exec("n")` → Busy carrying
>    the hint line; `peek("n")` → an int repr within 1 s.
> 2. `exec("2+2", queue=True, timeout_s=60)` behind a 10 s sleep cell returns `4` in one
>    call.
> 3. `peek("os.system('true')")` → rejected (call nodes refused), kernel untouched.
> 4. A dispatched subagent's `peek` answers from the subagent's own kernel.
> 5. `peek()` of an object whose `__repr__` blocks → a timeout error within ~5 s, and a
>    subsequent peek still answers.

- [ ] **Step 1: Full suite**

Run: `uv run pytest test/ -q` — paste the tail into the report.

- [ ] **Step 2: The acceptance criteria via their named test twins**

Each criterion has a test built in Tasks 2-4; run them by name as the acceptance record
(criterion → test):

Run:
`uv run pytest "test/integration/test_peek_live.py::test_peek_answers_under_a_busy_kernel_within_a_second" "test/unit/test_shape.py::test_busy_render_carries_the_queue_hint" "test/integration/test_busy_yield_wait.py::test_queued_exec_lands_behind_a_sleeping_cell" "test/unit/test_peek.py::test_validate_refuses_everything_else" "test/unit/test_peek.py::test_blocking_repr_times_out_and_channel_survives" test/integration/test_subagent_keying.py -v`

Expected: all pass. Criterion 2's live twin uses a 3 s sleep rather than 10 (same
mechanism, faster suite — the spec's 10 s is illustrative); note this deviation in the
report. Criterion 3's exact expression: additionally run a one-liner proof —

```bash
uv run python -c "
from ptc.runtime import peek
try:
    peek._validate(\"os.system('true')\")
    raise SystemExit('NOT REJECTED')
except ValueError as e:
    print('rejected as spec demands:', e)
"
```

Expected: `rejected as spec demands: … (refused: Call)`.

- [ ] **Step 3: Report**

Record: suite tail, each criterion PASS/FAIL with its test name, the criterion-2 timing
deviation, and any surprise.

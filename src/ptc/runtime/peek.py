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
from pathlib import Path

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
    must never fail bootstrap.

    The REAL socket binds at a short deterministic tmpdir path and `peek.sock` is a
    SYMLINK to it: AF_UNIX caps sun_path at 104 bytes on macOS, and a kernel dir under a
    pytest tmp_path (or a long PTC_HOME, or a subagent-suffixed key) blows past it —
    bind fails, the belt below swallows it, and peek is silently absent. connect()
    resolves symlinks, so the client and every consumer see only `peek.sock`. The real
    path is derived by hash, so a respawn of the same key lands on the same file and the
    unlink-before-bind covers the previous incarnation.
    """
    import hashlib
    import tempfile
    path = kernel_dir / "peek.sock"
    real = (Path(tempfile.gettempdir())
            / f"ptc-peek-{hashlib.sha256(str(path).encode()).hexdigest()[:12]}.sock")
    try:
        real.unlink(missing_ok=True)    # a previous incarnation's stale socket
        path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(real))
        os.chmod(real, 0o600)           # owner-only even in a world-writable tmpdir
        sock.listen(4)
        path.symlink_to(real)
    except OSError:
        return
    threading.Thread(target=_serve, args=(sock, namespace), daemon=True,
                     name="ptc-peek").start()

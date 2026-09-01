"""Peek: AST restriction, bounds, and the full socket loop against a plain dict —
no kernel needed (the daemon serves whatever namespace mapping it is handed)."""
import time
from pathlib import Path

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
    # the published name is a SYMLINK to a short real socket (AF_UNIX 104-byte cap —
    # this very tmp_path is longer than the cap, which is the point), owner-only
    import os as _os
    assert sock_path.is_symlink()
    assert (_os.stat(sock_path.resolve()).st_mode & 0o777) == 0o600


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


def test_accepted_but_unanswered_reads_as_unavailable_not_as_garbage(tmp_path):
    """A dying kernel accepts into its backlog and goes away before replying, so the
    client reads EOF with no bytes. That is 'nothing answered', not a malformed reply —
    the live dead-kernel case lands here whenever the process death races the connect."""
    import socket as _socket
    import tempfile
    import threading as _threading
    real = Path(tempfile.mkdtemp()) / "mute.sock"       # short: AF_UNIX's 104-byte cap
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(real))
    srv.listen(1)

    def _accept_then_vanish():
        conn = srv.accept()[0]
        conn.recv(4096)             # take the request, then die without replying
        conn.close()

    _threading.Thread(target=_accept_then_vanish, daemon=True).start()
    sock_path = tmp_path / "peek.sock"
    sock_path.symlink_to(real)
    with pytest.raises(peek_client.PeekUnavailable, match="sent no reply"):
        peek_client.peek_kernel_path(sock_path, "x", timeout_s=3.0)
    srv.close()


def test_client_connects_to_the_published_path_when_it_is_not_a_symlink(tmp_path):
    """The 104-byte AF_UNIX cap binds the CLIENT's connect() as well as the server's
    bind(), and this file's tmp_path is over it — so every round trip above already
    proves the client follows the symlink. This pins the other branch: a socket
    published directly at its own path has no link to follow."""
    plain = tmp_path / "plain.sock"
    plain.touch()
    assert peek_client._connect_path(plain) == str(plain)


def test_mutation_is_inexpressible_but_getattr_runs_user_code(tmp_path):
    """The honest claim (spec): mutation-RESISTANT, not read-only — property access runs
    user code; what the grammar refuses is calls and assignments."""
    class Trap:
        @property
        def boom(self):
            return "property ran"
    sock_path = _daemon(tmp_path, {"t": Trap()})
    assert peek_client.peek_kernel_path(sock_path, "t.boom")["repr"] == "'property ran'"

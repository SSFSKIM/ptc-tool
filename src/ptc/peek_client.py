"""Adapter/CLI-side reader for a kernel's peek socket."""
import json
import os
import socket

from .paths import kernel_dir


class PeekUnavailable(RuntimeError):
    """No socket to ask: the kernel predates peek, or nothing is answering."""


def _connect_path(sock_path) -> str:
    """The path to hand connect(). The 104-byte AF_UNIX cap on sun_path binds BOTH ends,
    so following the server's symlink is the client's half of the same accommodation:
    `peek.sock` under a long kernel dir is itself over the cap, and connect() would fail
    with "AF_UNIX path too long" before it ever reached the short socket the server bound.
    readlink rather than resolve() — resolve() also rewrites a non-symlink to its realpath,
    which on macOS prepends /private and can push a path that fit over the cap.
    """
    try:
        return os.readlink(sock_path)
    except OSError:
        return str(sock_path)           # not a symlink: the published path IS the socket


def peek_kernel_path(sock_path, expr: str, timeout_s: float = 6.0) -> dict:
    if not sock_path.exists():
        raise PeekUnavailable(f"no peek socket at {sock_path}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        try:
            s.connect(_connect_path(sock_path))
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
    if not data:
        # An accept with no reply is the DYING kernel's signature: the listener took the
        # connection into its backlog and the process went away before answering, so the
        # read ends at EOF (or, if nothing closes the fd, at the timeout). Either way
        # nothing answered — reporting it as a malformed reply would send a reader
        # looking for a protocol bug that is not there.
        raise PeekUnavailable(f"peek socket accepted but sent no reply: {sock_path}")
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise PeekUnavailable(f"peek socket answered garbage: {e}") from e


def peek_kernel(key: str, expr: str, timeout_s: float = 6.0) -> dict:
    """The keyed form the MCP tool and CLI use; `timeout_s` bounds the whole round trip
    (the server joins its eval thread at 5 s, so 6 covers it)."""
    return peek_kernel_path(kernel_dir(key) / "peek.sock", expr, timeout_s)

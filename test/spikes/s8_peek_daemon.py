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
        "cached test venv missing — run `uv run pytest test/integration/test_kernel_lifecycle.py -q` once first (it builds .venv-kernel via the kernel_venv fixture)"
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
import hashlib, tempfile
_p = %r
_real = os.path.join(tempfile.gettempdir(),
                     "ptc-peek-" + hashlib.sha256(_p.encode()).hexdigest()[:12] + ".sock")
for _q in (_real, _p):
    try: os.unlink(_q)
    except OSError: pass
_s = socket.socket(socket.AF_UNIX); _s.bind(_real); os.chmod(_real, 0o600); _s.listen(4)
os.symlink(_real, _p)   # AF_UNIX 104-byte cap: publish a symlink, bind short (ships as-is)
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

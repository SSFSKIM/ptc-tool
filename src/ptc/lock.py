"""flock-based mutual exclusion. POSIX only (spec: no Windows in v1)."""
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import kernel_dir


@contextmanager
def flock_path(path: Path, timeout: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"lock busy: {path}")
                    time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def key_lock(key: str):
    return flock_path(kernel_dir(key) / "lock")


def submit_lock(key: str):
    return flock_path(kernel_dir(key) / "submit.lock", timeout=10.0)

"""flock-based mutual exclusion. POSIX only (spec: no Windows in v1)."""
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import kernel_dir


@contextmanager
def flock_path(path: Path, timeout: float | None = None):
    """Hold an exclusive flock on `path` — on the file that is AT `path` once it is held.

    A lock file can be unlinked while held: kernel-directory GC removes a dead key's whole
    directory, lock included, under that lock. A caller already blocked on the old file
    then wakes holding a lock on an inode nothing can reach any more, while the next caller
    creates a fresh file at the path and locks that — two holders for one key. So a won
    lock is checked against the path (same device and inode) and, if the file was replaced
    or removed meanwhile, dropped and retaken on whatever stands there now.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if deadline is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"lock busy: {path}")
                        time.sleep(0.05)
            if _same_file(fd, path):
                break
        except BaseException:
            os.close(fd)
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _same_file(fd: int, path: Path) -> bool:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (held.st_dev, held.st_ino) == (st.st_dev, st.st_ino)


def key_lock(key: str):
    return flock_path(kernel_dir(key) / "lock")


def submit_lock(key: str):
    return flock_path(kernel_dir(key) / "submit.lock", timeout=10.0)

"""owner.json: which OS process is this key's kernel. Identity = pid + start time."""
import json
import os
import subprocess
from dataclasses import asdict, dataclass

from .paths import kernel_dir


def proc_start_time(pid: int) -> str | None:
    """`ps -o lstart=` works on macOS and Linux procps; None if unreadable."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        s = out.stdout.strip()
        return s or None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class Owner:
    pid: int
    proc_start_time: str | None
    spawned_at: float
    nonce: str
    epoch: str


def write_owner(key: str, o: Owner) -> None:
    d = kernel_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "owner.json.tmp"
    tmp.write_text(json.dumps(asdict(o)))
    tmp.replace(d / "owner.json")


def read_owner(key: str) -> Owner | None:
    p = kernel_dir(key) / "owner.json"
    try:
        return Owner(**json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def owner_alive(o: Owner) -> bool:
    try:
        os.kill(o.pid, 0)
    except OSError:
        return False
    st = proc_start_time(o.pid)
    return st is not None and o.proc_start_time is not None and st == o.proc_start_time

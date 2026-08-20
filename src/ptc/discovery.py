"""Session-key discovery (T13) + kernel meta.json (T4)."""
import json
from .paths import kernel_dir


def write_meta(key: str, **fields) -> None:
    d = kernel_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    merged = read_meta(key)
    merged.update(fields)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(merged))
    tmp.replace(d / "meta.json")


def read_meta(key: str) -> dict:
    try:
        return json.loads((kernel_dir(key) / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}

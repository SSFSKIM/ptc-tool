"""Pure file-level access to per-cell logs, records, current.json, offsets."""
import json
from dataclasses import dataclass

from .paths import cells_dir, secure_dir


@dataclass
class CellRecord:
    status: str
    duration_ms: int
    result_repr: str | None
    error: dict | None
    images: list
    mutations: list


def read_record(key: str, cell_id: int) -> CellRecord | None:
    p = cells_dir(key) / f"{cell_id}.json"
    try:
        d = json.loads(p.read_text())
        return CellRecord(**d)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def read_output_since(key: str, cell_id: int, offset: int,
                      max_bytes: int = 4_000_000) -> tuple[str, int]:
    p = cells_dir(key) / f"{cell_id}.log"
    try:
        with open(p, "rb") as f:
            f.seek(max(offset, 0))
            data = f.read(max_bytes)
            return data.decode(errors="replace"), f.tell()
    except OSError:
        return "", max(offset, 0)


def current_cell(key: str) -> int | None:
    try:
        return json.loads((cells_dir(key) / "current.json").read_text())["cell_id"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def default_offset(key: str, cell_id: int) -> int:
    try:
        return int((cells_dir(key) / "offsets" / f"{cell_id}.offset").read_text())
    except (OSError, ValueError):
        return 0


def save_offset(key: str, cell_id: int, offset: int) -> None:
    d = secure_dir(cells_dir(key) / "offsets")
    (d / f"{cell_id}.offset").write_text(str(offset))

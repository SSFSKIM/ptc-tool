from ptc.cells import CellRecord
from ptc.client import Busy, Completed, Running
from ptc.paths import Config
from ptc.shape import footer_line, render


def _rec(**kw):
    base = dict(status="ok", duration_ms=1234, result_repr=None, error=None, images=[], mutations=[])
    base.update(kw)
    return CellRecord(**base)


def test_render_completed_ok():
    out = Completed(14, _rec(result_repr="42"), "hello\n")
    r = render(out, "k", Config.from_env(env={}))
    assert r.text.startswith("[cell 14 · ok · 1.2s]")
    assert "hello" in r.text and "→ result: 42" in r.text


def test_render_truncates_head_tail():
    big = "x" * 100_000
    out = Completed(3, _rec(), big)
    cfg = Config.from_env(env={"PTC_MAX_OUTPUT_CHARS": "1000"})
    r = render(out, "k", cfg)
    assert len(r.text) < 3000
    assert "[truncated" in r.text and "cells/3.log" in r.text
    assert r.text.count("x" * 100) >= 2          # head and tail both survive


def test_render_running_busy_and_degraded():
    r = render(Running(5, "partial", 77), "k", Config.from_env(env={}))
    assert "[cell 5 · running" in r.text and "wait" in r.text and "77" in r.text
    b = render(Busy(9), "k", Config.from_env(env={}), degraded=True)
    assert "busy" in b.text and "9" in b.text and "[keying: adapter-local]" in b.text


def test_footer_and_error():
    ms = [{"kind": "edit", "path": "src/a.py", "added": 3, "removed": 1},
          {"kind": "write", "path": "out.md", "added": 10},
          {"kind": "bash", "command": "npm test"}]
    f = footer_line(ms)
    assert "edited src/a.py (+3/−1)" in f and "wrote out.md" in f and "ran: npm test" in f
    out = Completed(2, _rec(status="error",
                            error={"ename": "ValueError", "evalue": "bad", "traceback": "tb"}), "")
    r = render(out, "k", Config.from_env(env={}))
    assert "[cell 2 · error" in r.text and "ValueError: bad" in r.text

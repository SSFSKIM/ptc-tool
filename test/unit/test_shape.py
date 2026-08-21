from ptc.cells import CellRecord
from ptc.client import Busy, Completed, Running
from ptc.paths import Config
from ptc.shape import footer_line, render, to_dict


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


# --- self-review extension: Busy carries (cell_id, reason) since T6 — "running",
# "pending-unconfirmed", "lock-held". None/-1 mean no real id is known; the three
# reasons must render distinct guidance and never fabricate an id (client.py docs
# this as the tri-state contract shape.render() must honor).

def test_render_busy_reasons_distinguish_without_fabricating_ids():
    cfg = Config.from_env(env={})

    running = render(Busy(7, reason="running"), "k", cfg)
    assert "cell 7 is still running" in running.text
    assert "wait(cell_id=7)" in running.text

    pending_known = render(Busy(7, reason="pending-unconfirmed"), "k", cfg)
    assert "confirmation" in pending_known.text and "wait(cell_id=7)" in pending_known.text

    # -1 is the wedged sentinel (client.py): the kernel never echoed execute_input,
    # so no real id exists yet — must not be printed or handed to wait().
    pending_sentinel = render(Busy(-1, reason="pending-unconfirmed"), "k", cfg)
    assert "wait(cell_id=" not in pending_sentinel.text
    assert "-1" not in pending_sentinel.text

    lock_known = render(Busy(7, reason="lock-held"), "k", cfg)
    assert "admission lock" in lock_known.text and "wait(cell_id=7)" in lock_known.text

    # our own lock acquisition timed out and is_busy() found nothing conclusive:
    # genuinely no id, must not print "None" or invent one.
    lock_unknown = render(Busy(None, reason="lock-held"), "k", cfg)
    assert "wait(cell_id=" not in lock_unknown.text and "None" not in lock_unknown.text

    # the two id-less reasons must not collapse into identical guidance
    assert pending_sentinel.text != lock_unknown.text


def test_truncate_non_positive_cap_still_truncates():
    """PTC_MAX_OUTPUT_CHARS=0 must not defeat truncation: Python's text[-0:] is the
    WHOLE string (not empty), so a naive tail slice would leak everything back out
    exactly when the operator asked for the strictest cap."""
    big = "y" * 500
    out = Completed(4, _rec(), big)
    cfg = Config.from_env(env={"PTC_MAX_OUTPUT_CHARS": "0"})
    r = render(out, "k", cfg)
    assert ("y" * 500) not in r.text
    assert "[truncated 500 chars" in r.text


def test_footer_joins_the_same_response_budget():
    """The mutation footer is part of the response budget, not a tail appended after it.

    Rendered last and unbounded, a cell that wrote ten thousand files added ~179k
    characters AFTER the output had been truncated to the caller's cap — the cap the
    caller asked for, bypassed by the one line nobody bounded.
    """
    ms = [{"kind": "write", "path": f"generated/file-{i:05d}.txt"} for i in range(10_000)]
    cfg = Config.from_env(env={"PTC_MAX_OUTPUT_CHARS": "2000"})
    r = render(Completed(7, _rec(mutations=ms), "out\n"), "k", cfg)

    assert len(r.text) < 2_000, f"footer bypassed the cap: {len(r.text)} chars"
    assert "wrote generated/file-00000.txt" in r.text     # whole entries, never a
    assert "wrote generated/file-0000" in r.text          # half-written path
    assert "more mutations" in r.text

    # what the summary claims must be true: kept entries + summarized count == all of them
    footer = r.text.splitlines()[-1]
    kept = [p for p in footer.split(" · ") if p.startswith("wrote ")]
    summarized = int(footer.rsplit("… and ", 1)[1].split(" ")[0])
    assert len(kept) + summarized == 10_000

    # small counts are byte-identical to before: the common cell sees no change at all
    assert footer_line(ms[:2]) == ("wrote generated/file-00000.txt · "
                                   "wrote generated/file-00001.txt")


def test_to_dict_busy_carries_reason():
    """to_dict is the JSON-facing twin of render(); a --json/MCP caller needs the
    same reason distinction a text reader gets from the prose."""
    d = to_dict(Busy(None, reason="lock-held"), "k")
    assert d == {"status": "busy", "cell_id": None, "reason": "lock-held"}


def test_result_and_error_ride_inside_the_response_budget():
    """`result_repr` (up to 4096 chars from the kernel) and the error summary are appended
    AFTER the body has spent its budget, so a 100-character cap still returned over 4 KB —
    the same bypass the mutation footer had, from the other end of the render."""
    cfg = Config.from_env(env={"PTC_MAX_OUTPUT_CHARS": "100"})
    rec = _rec(status="error", result_repr="R" * 4096,
               error={"ename": "ValueError", "evalue": "E" * 2000, "traceback": "tb"})
    r = render(Completed(8, rec, "body\n"), "k", cfg)

    assert len(r.text) < 800, f"the cap was bypassed: {len(r.text)} chars"
    assert r.text.startswith("[cell 8 · error"), "the header must survive intact"
    assert "ValueError: EE" in r.text and "→ result: RR" in r.text     # both still SAID
    assert r.text.count("…") >= 2, "both were clipped, and say so"

    # small cases are byte-identical: nothing at or under the cap is touched at all
    small = Config.from_env(env={})
    plain = render(Completed(8, _rec(result_repr="42"), "hi\n"), "k", small)
    assert plain.text == "[cell 8 · ok · 1.2s]\nhi\n→ result: 42"

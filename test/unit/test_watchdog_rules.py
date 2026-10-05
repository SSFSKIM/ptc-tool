"""The watchdog's expiry rules beyond the plain TTL.

A subagent kernel was observed holding 5.2 GB for the full hour of its TTL after the run
that loaded it had finished, on a machine already swapping. Two things the kernel can know
end that sooner: its subagent has stopped (the SubagentStop hook's marker), and it is idle
while holding a lot of memory — stricter still while the system is short of it. None of
the rules may touch a kernel with a cell in flight.

`bootstrap._expiry` is pure, so the rules are pinned here without a kernel; the end-to-end
expiries are in test/integration/test_ttl.py.
"""
import os
import subprocess
import sys
import time

import pytest

from ptc import memory
from ptc.paths import Config
from ptc.runtime import bootstrap
from ptc.runtime.bootstrap import _expiry, _Rules
from ptc.runtime.state import STATE

MB = 2**20
NOW = 1_000_000.0


def _rules(**over) -> _Rules:
    cfg = {"idle_hours": 24, "stop_grace_min": 10, "heavy_mb": 1024, "heavy_idle_min": 30,
           "pressure_mb": 512, "pressure_idle_min": 5}
    cfg.update(over)
    return _Rules.from_config(cfg)


def _why(idle_s, *, rules=None, stopped_ago=None, held=None, pressured=False):
    """The verdict for a kernel idle `idle_s`, stopped `stopped_ago` seconds ago, holding
    `held` bytes. The callables record whether they were consulted."""
    asked = []

    def footprint():
        asked.append("footprint")
        return held

    def under_pressure():
        asked.append("pressure")
        return pressured

    reason = _expiry(NOW, rules or _rules(), last_activity=NOW - idle_s,
                     stopped_at=None if stopped_ago is None else NOW - stopped_ago,
                     footprint=footprint, pressured=under_pressure)
    return reason, asked


def test_rules_read_the_spawn_config_and_default_to_configs_own():
    r = _rules()
    assert (r.ttl_s, r.stop_grace_s) == (24 * 3600, 600)
    assert (r.heavy_bytes, r.heavy_idle_s) == (1024 * MB, 1800)
    assert (r.pressure_bytes, r.pressure_idle_s) == (512 * MB, 300)
    # a payload from before a field existed gets Config's default, not a crash
    assert _Rules.from_config({}) == _Rules.from_config(
        {f: getattr(Config, f) for f in ("idle_hours", "stop_grace_min", "heavy_mb",
                                         "heavy_idle_min", "pressure_mb",
                                         "pressure_idle_min")})


def test_a_non_positive_threshold_turns_its_rule_off():
    r = _rules(heavy_mb=0, pressure_mb=-1)
    assert r.heavy_bytes is None and r.pressure_bytes is None
    reason, asked = _why(3 * 3600, rules=r, held=50 * 2**30, pressured=True)
    assert reason is None and asked == [], "a disabled rule measured or expired anyway"


def test_the_plain_ttl_is_unchanged():
    assert _why(24 * 3600 + 1)[0].startswith("expired after 24.00 h idle")
    assert _why(24 * 3600 - 1, held=10 * MB)[0] is None


# --- the stopped subagent -------------------------------------------------------------

def test_a_stopped_subagents_kernel_goes_after_the_grace_counted_from_the_stop():
    """Counted from the STOP: a subagent that spent its last stretch on other tools still
    gets the whole grace for a SendMessage continuation."""
    reason, _ = _why(3000, stopped_ago=601)
    assert reason is not None and "subagent stopped" in reason
    assert _why(3000, stopped_ago=599)[0] is None, "expired inside the grace"


def test_a_cell_after_the_stop_restores_the_normal_ttl():
    """The marker counts only while it is newer than the last activity: a continued
    subagent that ran a cell is using its kernel again."""
    reason, _ = _why(700, stopped_ago=1000)          # last cell 700 s ago, stop 1000 s ago
    assert reason is None


def test_no_marker_means_no_grace():
    assert _why(3000)[0] is None


# --- heavy and pressured ----------------------------------------------------------------

def test_a_heavy_kernel_goes_after_the_heavy_window():
    reason, _ = _why(1801, held=1024 * MB)
    assert reason is not None and "holding 1.0G" in reason and "pressure" not in reason
    assert _why(1799, held=5 * 2**30)[0] is None, "heavy, but not idle long enough"
    assert _why(1801, held=1023 * MB)[0] is None, "idle long enough, but not heavy"


def test_under_pressure_the_window_and_the_threshold_both_tighten():
    reason, _ = _why(301, held=512 * MB, pressured=True)
    assert reason is not None and "memory pressure" in reason
    assert _why(301, held=512 * MB, pressured=False)[0] is None
    assert _why(299, held=900 * MB, pressured=True)[0] is None
    assert _why(301, held=511 * MB, pressured=True)[0] is None


def test_memory_is_measured_only_once_idle_makes_it_matter():
    """The footprint and the pressure level are read lazily: a busy kernel's watchdog
    tick costs nothing, and pressure is asked only of a kernel already over its line."""
    assert _why(10, held=50 * 2**30)[1] == []
    assert _why(301, held=100 * MB)[1] == ["footprint"]
    assert _why(301, held=600 * MB)[1] == ["footprint", "pressure"]


def test_an_unmeasurable_footprint_is_not_heavy():
    assert _why(3 * 3600, held=None, pressured=True)[0] is None


def test_the_tick_follows_the_shortest_live_window():
    assert _rules().tick() == 30.0
    assert _rules(idle_hours=0.001).tick() == pytest.approx(0.5)
    assert _rules(heavy_idle_min=0.1).tick() == pytest.approx(0.6)
    # a disabled rule's window does not make the watchdog spin
    assert _rules(heavy_mb=0, heavy_idle_min=0.001).tick() == 30.0


# --- in flight --------------------------------------------------------------------------

@pytest.fixture
def kernel_state(monkeypatch, tmp_path):
    (tmp_path / "cells").mkdir()
    monkeypatch.setattr(STATE, "kernel_dir", tmp_path)
    monkeypatch.setattr(STATE, "current_cell", None)
    return tmp_path


def test_no_rule_touches_a_cell_in_flight(kernel_state, monkeypatch):
    """`last_activity` is stamped at cell START, so a long cell looks idle to every rule:
    stopped, heavy and pressured all at once must still wait for its record."""
    (kernel_state / bootstrap.STOP_MARKER).write_text("{}")
    monkeypatch.setattr(STATE, "last_activity", 0.0)           # "idle" since 1970
    monkeypatch.setattr(bootstrap, "footprint", lambda: 50 * 2**30)
    monkeypatch.setattr(bootstrap, "under_pressure", lambda: True)
    monkeypatch.setattr(STATE, "current_cell", 7)
    rules = _rules(idle_hours=0.001, stop_grace_min=0.001)
    assert bootstrap._sample(rules) is None
    (kernel_state / "cells" / "7.json").write_text("{}")       # the record lands
    assert bootstrap._sample(rules) is not None


def test_the_marker_is_read_from_the_kernel_directory(kernel_state, monkeypatch):
    monkeypatch.setattr(bootstrap, "footprint", lambda: 0)
    rules = _rules(stop_grace_min=1)
    now = time.time()
    monkeypatch.setattr(STATE, "last_activity", now - 3000)
    marker = kernel_state / bootstrap.STOP_MARKER
    marker.write_text("{}")
    os.utime(marker, (now - 2000, now - 2000))  # stopped after the last cell, long ago
    reason, _ = bootstrap._sample(rules)
    assert "subagent stopped" in reason


# --- the decision is atomic with cell admission ---------------------------------------

@pytest.fixture
def expiring(kernel_state, monkeypatch, tmp_path):
    """A kernel idle long past a heavy window, with the exit replaced by a recorder. The
    key lock is real: it lives under PTC_HOME, beside the kernel directory."""
    home = tmp_path / "home"
    monkeypatch.setenv("PTC_HOME", str(home))
    (home / "kernels" / "k").mkdir(parents=True)
    monkeypatch.setattr(STATE, "key", "k")
    monkeypatch.setattr(STATE, "last_activity", time.time() - 3600)
    exits = []
    monkeypatch.setattr(bootstrap, "_reap_and_exit", lambda: exits.append(True))
    return exits


def test_a_cell_admitted_while_the_footprint_is_read_is_not_killed(expiring, kernel_state,
                                                                    monkeypatch):
    """The footprint and pressure reads let other threads run, and `_pre_run_cell` admits
    a cell through the submit lock without ever taking the key lock — so a cell started
    mid-sample was killed on a verdict about an idle kernel. The admission happens inside
    the sample here, deterministically."""
    def footprint_admitting_a_cell():
        bootstrap._admit(9)
        return 50 * 2**30

    monkeypatch.setattr(bootstrap, "footprint", footprint_admitting_a_cell)
    bootstrap._expire_if_idle(_rules(heavy_idle_min=1))
    assert expiring == [], "the watchdog exited under a cell it had just let start"
    assert not (kernel_state / "expired.marker").exists()


def test_an_idle_heavy_kernel_still_goes(expiring, kernel_state, monkeypatch):
    monkeypatch.setattr(bootstrap, "footprint", lambda: 50 * 2**30)
    bootstrap._expire_if_idle(_rules(heavy_idle_min=1))
    assert expiring == [True]
    assert "of memory" in (kernel_state / "expired.marker").read_text()


def test_no_cell_can_start_between_the_final_check_and_the_exit(expiring, monkeypatch):
    """Once the watchdog has committed, a cell arriving waits on admission until the exit
    takes it — it never starts in a kernel that is already going."""
    import threading

    monkeypatch.setattr(bootstrap, "footprint", lambda: 50 * 2**30)
    started = []

    def exit_while_a_cell_arrives():
        t = threading.Thread(target=lambda: (bootstrap._admit(9), started.append(9)))
        t.start()
        t.join(0.3)
        expiring.append(not started)            # the arriving cell was held off
        exit_while_a_cell_arrives.thread = t

    monkeypatch.setattr(bootstrap, "_reap_and_exit", exit_while_a_cell_arrives)
    bootstrap._expire_if_idle(_rules(heavy_idle_min=1))
    exit_while_a_cell_arrives.thread.join(5)    # the stub returns, so admission frees
    assert expiring == [True], "a cell started after the watchdog committed to exiting"


# --- the measurements themselves -------------------------------------------------------

@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX footprint only")
def test_footprint_reads_this_process_and_another():
    own = memory.footprint()
    assert own is not None and own > 1 * MB
    p = subprocess.Popen([sys.executable, "-c",
                          "b = b\"\\x01\" * (200 * 2**20); import time; print(1, flush=True); "
                          "time.sleep(30)"], stdout=subprocess.PIPE)
    try:
        p.stdout.readline()
        other = memory.footprint(p.pid)
        assert other is not None and other >= 190 * MB
    finally:
        p.kill()
        p.wait()
    assert memory.footprint(p.pid) is None, "a reaped pid still reported a footprint"


def test_pressure_is_a_plain_bool():
    assert memory.under_pressure() in (True, False)


def test_human_sizes():
    assert memory.human(5_583_457_484) == "5.2G"
    assert memory.human(740 * MB) == "740M"

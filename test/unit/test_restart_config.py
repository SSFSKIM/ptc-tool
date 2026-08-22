"""A restart restores the config the KERNEL lives under, not the restarter's environment.

`Config` carries two kinds of field. Four of them are consumed once, kernel-side, at the
spawn — the TTL watchdog's `idle_hours`, the shared semaphore's `max_concurrency` and the
recursion brake's `depth`/`max_depth` — and they are properties of the kernel for the whole
of its life. The other two, `yield_s` and `max_output_chars`, are read per call by the
client and the renderer and belong to whoever is asking.

`restart_kernel` rebuilt the whole of it from the restarting process's environment, so a
kernel created with a 30-minute TTL and a concurrency of 2 came back from a restart in a
plain shell with the 24-hour default and a bound of 8. r8 and r15 restored the brake; these
tests pin the rest of the kernel-lifetime set alongside it, and pin the caller-side knobs
staying the caller's.
"""
import time

import pytest

from ptc import kernel
from ptc.discovery import write_meta
from ptc.paths import Config, kernel_dir, secure_dir


@pytest.fixture
def restarted(monkeypatch, tmp_path):
    """`restart_kernel` with the kill and the respawn stubbed: returns the Config the
    respawn was handed."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr(kernel, "kill_kernel", lambda key: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    seen: dict = {}

    def fake_ensure(key, **kw):
        seen.update(kw)
        return kw

    monkeypatch.setattr(kernel, "ensure_kernel", fake_ensure)

    def run(key: str, **kw) -> Config:
        secure_dir(kernel_dir(key))
        kernel.restart_kernel(key, **kw)
        return seen["config"]

    return run


def _plain_env(monkeypatch) -> None:
    """The restarting process: a terminal that never heard of this kernel's settings."""
    for name in ("PTC_IDLE_HOURS", "PTC_MAX_CONCURRENCY", "PTC_DEPTH", "PTC_MAX_DEPTH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PTC_YIELD_S", "42")
    monkeypatch.setenv("PTC_MAX_OUTPUT_CHARS", "99")


def test_the_kernel_lifetime_config_travels_with_the_kernel(monkeypatch, restarted):
    _plain_env(monkeypatch)
    write_meta("rc1", depth=1, max_depth=2, idle_hours=0.5, max_concurrency=2)

    cfg = restarted("rc1")

    assert (cfg.idle_hours, cfg.max_concurrency) == (0.5, 2), \
        "the restart reset the TTL and the concurrency bound to the restarter's defaults"
    assert (cfg.depth, cfg.max_depth) == (1, 2), "the recursion brake was not restored"


def test_the_per_call_knobs_stay_the_callers(monkeypatch, restarted):
    """`yield_s` and `max_output_chars` are read by the client and the renderer on every
    request, never by the kernel. Pinning them to the spawning caller's values would make a
    restart silently change what a LATER, unrelated caller's timeout means."""
    _plain_env(monkeypatch)
    write_meta("rc2", idle_hours=0.5, max_concurrency=2,
               yield_s=1.0, max_output_chars=7)

    cfg = restarted("rc2")

    assert (cfg.yield_s, cfg.max_output_chars) == (42.0, 99), \
        "a per-call knob was restored from the kernel's metadata"


def test_a_field_absent_from_older_metadata_keeps_the_environment_value(monkeypatch,
                                                                       restarted):
    """Migration by ABSENCE, per field and with no version marker — the same stance r15
    took for `max_depth`. A meta.json written before these fields were recorded keeps the
    behaviour it has today for exactly the fields it does not carry."""
    _plain_env(monkeypatch)
    monkeypatch.setenv("PTC_MAX_CONCURRENCY", "5")
    write_meta("rc3", idle_hours=0.5)

    cfg = restarted("rc3")

    assert cfg.idle_hours == 0.5, "the recorded field was not restored"
    assert cfg.max_concurrency == 5, "an unrecorded field did not fall through to the env"


def test_an_explicit_config_still_supplies_everything_meta_does_not(monkeypatch, restarted):
    """A caller that passes `config=` supplies the whole configuration, and it is used —
    but the fields the kernel itself was created with still win over it.

    This is the r15 rule applied uniformly rather than only to the brake. `restart_kernel`
    has no in-tree caller that passes a config, and the natural one to write would be
    `restart_kernel(key, config=Config.from_env())` — the very construction that reopens
    the hole, since the object then carries the restarter's environment inside it.
    """
    _plain_env(monkeypatch)
    write_meta("rc4", depth=1, max_depth=2, idle_hours=0.5, max_concurrency=2)

    cfg = restarted("rc4", config=Config(yield_s=7.0, max_output_chars=8,
                                         idle_hours=24.0, max_concurrency=8))

    assert (cfg.yield_s, cfg.max_output_chars) == (7.0, 8), "the explicit config was ignored"
    assert (cfg.idle_hours, cfg.max_concurrency) == (0.5, 2), \
        "an explicit config released the kernel's own lifetime settings"
    assert (cfg.depth, cfg.max_depth) == (1, 2), \
        "an explicit config released the recursion brake"

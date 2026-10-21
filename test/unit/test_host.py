"""`AgentHost` against a fake mod written in Python (`host_fakes.py`): the channel protocol,
the job table, the host semaphore, generations and their retirement, the crash loop.

No `claude` process and no model call: the launch is `FakeLauncher`, and the fake mod
speaks protocol 1 over the real Unix socket the host serves.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from host_fakes import FakeLauncher, echo, hold, release

from ptc.runtime import host as host_mod
from ptc.runtime.agents import AgentFailed
from ptc.runtime.host import AgentHost, HostUnavailable
from ptc.runtime.state import STATE


@pytest.fixture(autouse=True)
def _kernel_state(tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "host-test", "depth": 0, "max_depth": 1, "max_concurrency": 8}


def make_host(tmp_path, launcher, **kw) -> AgentHost:
    kw.setdefault("start_s", 5.0)
    return AgentHost(kernel_dir=tmp_path, cwd=str(tmp_path), plugin_dir=tmp_path / "plugin",
                     launch=launcher, **kw)


def complete(prompt="hi", **kw) -> dict:
    return {"kind": "complete", "model": "haiku", "system": "be terse", "prompt": prompt,
            "maxTokens": 64, "effort": None, **kw}


def agent_job(prompt="look") -> dict:
    return {"kind": "agent", "type": {"prompt": "read only", "tools": ["Read"],
                                      "model": "haiku", "effort": None,
                                      "permissionMode": "bypassPermissions", "maxTurns": 50},
            "prompt": prompt, "cwd": None, "description": "ptc agent"}


def soon(seconds: float) -> float:
    return time.monotonic() + seconds


async def until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


# ---- start, hello, protocol --------------------------------------------------------------


def test_a_job_round_trips_through_the_channel(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            r = await h.submit(complete("ping"), soon(5))
            assert r["ok"] and r["text"] == "echo:ping"
            [job] = launcher.mod.jobs
            assert job["id"].startswith("j-") and len(job["id"]) == 2 + 32
            assert job["timeoutMs"] is not None and 0 < job["timeoutMs"] <= 5000
            assert h.session_id == "host-session" and h.host_cwd == "/fake/cwd"
        finally:
            await h.close()
    asyncio.run(main())


def test_the_hosts_environment(tmp_path, monkeypatch):
    """The launch recipe's variables (M1 verdict): function hooks on, the socket's short
    real path, the engine's subagent caps above the kernel's, git context off, and the
    variables that would force a model or load the session's mods emptied."""
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "opus")

    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher, cap=16)
        try:
            await h.ensure_started()
            env = launcher.envs[0]
            assert env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "1"
            assert env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == str(16 + 8)
            assert int(env["CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"]) >= 10**6
            assert env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] == "1"
            for k in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
                      "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL", "CLAUDE_CODE_PLUGIN_DIRS"):
                assert env[k] == ""
            sock = env["PTC_HOST_SOCKET"]
            assert len(sock.encode()) < 104 and Path(sock).is_socket()
            assert env["PTC_DEPTH"] == "1" and env["PTC_SESSION"]
        finally:
            await h.close()
    asyncio.run(main())


def test_a_protocol_mismatch_fails_the_start(tmp_path):
    async def main():
        launcher = FakeLauncher(protocol=2)
        h = make_host(tmp_path, launcher)
        try:
            with pytest.raises(HostUnavailable, match="protocol"):
                await h.ensure_started()
            assert launcher.mod.hello_reply[0] == 409
            assert launcher.mod.hello_reply[1] == {"ok": False, "reason": "protocol"}
            assert launcher.procs[0].closed
        finally:
            await h.close()
    asyncio.run(main())


def test_no_hello_within_the_start_bound_fails_the_start(tmp_path):
    async def main():
        async def silent(env, cwd, plugin_dir):
            class Mute:
                pid = 1
                closed = False

                async def wait(self):
                    await asyncio.sleep(3600)

                async def close(self):
                    self.closed = True
            silent.proc = Mute()
            return silent.proc
        h = make_host(tmp_path, silent, start_s=0.3)
        try:
            with pytest.raises(HostUnavailable, match="hello"):
                await h.ensure_started()
            assert silent.proc.closed
        finally:
            await h.close()
    asyncio.run(main())


def test_concurrent_callers_share_one_start(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            rs = await asyncio.gather(*[h.submit(complete(str(i)), soon(5)) for i in range(20)])
            assert [r["text"] for r in rs] == [f"echo:{i}" for i in range(20)]
            assert len(launcher.procs) == 1
        finally:
            await h.close()
    asyncio.run(main())


def test_a_held_poll_is_answered_at_once_on_a_queued_job(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            await h.ensure_started()
            await asyncio.sleep(0.3)               # the mod's poll is now held (25 s)
            t = time.monotonic()
            await h.submit(complete(), soon(5))
            assert time.monotonic() - t < 1.0
        finally:
            await h.close()
    asyncio.run(main())


# ---- results ------------------------------------------------------------------------------


def test_a_failed_result_raises_agent_failed_naming_its_reason(tmp_path):
    async def failing(mod, job):
        return {"ok": False, "reason": "api-error", "detail": "rate limited", "status": 429}

    async def main():
        h = make_host(tmp_path, FakeLauncher(handler=failing))
        try:
            with pytest.raises(AgentFailed, match=r"api-error.*429.*rate limited"):
                await h.submit(complete(), soon(5))
        finally:
            await h.close()
    asyncio.run(main())


def test_a_settled_or_unknown_id_gets_404_and_changes_nothing(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            await h.submit(complete(), soon(5))
            mod = launcher.mod
            [job] = mod.jobs
            assert await mod.post(job["id"], {"ok": True, "text": "again"}) == 404
            assert await mod.post("j-" + "0" * 32, {"ok": True, "text": "?"}) == 404
            assert h.held_slots() == 0
        finally:
            await h.close()
    asyncio.run(main())


def test_a_large_prompt_spills_to_a_file_and_a_large_result_comes_back_through_one(tmp_path):
    """Both directions of the large-payload path. The prompt is newline-heavy: small raw,
    large once JSON escapes it, so only the encoded size can tell it must spill."""
    prompt = "\n" * 1_800_000                     # 1.8 MB raw, 3.6 MB encoded
    big_reply = "y" * 3_600_000

    async def reply_big(mod, job):
        assert job["prompt"] is None and job["promptFile"]
        assert mod.text_of(job, "prompt") == prompt
        return {"ok": True, "text": big_reply}

    async def main():
        h = make_host(tmp_path, FakeLauncher(handler=reply_big))
        try:
            r = await h.submit(complete(prompt), soon(10))
            assert r["text"] == big_reply and r.get("textFile") is None
            assert list((tmp_path / "host" / "out").iterdir()) == []   # read, then deleted
            assert list((tmp_path / "host" / "in").iterdir()) == []    # released with the job
        finally:
            await h.close()
    asyncio.run(main())


def test_a_small_prompt_stays_inline(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            await h.submit(complete("x" * 100_000), soon(5))
            assert launcher.mod.jobs[0]["promptFile"] is None
        finally:
            await h.close()
    asyncio.run(main())


def test_a_field_over_the_read_cap_is_refused_before_it_is_queued(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            with pytest.raises(ValueError, match="4 MiB"):
                await h.submit(complete("é" * 2_200_000), soon(5))   # 4.4 MB in UTF-8
            with pytest.raises(ValueError, match="4 MiB"):
                await h.submit(complete("ok", system="s" * (4 * 2**20 + 1)), soon(5))
            assert launcher.procs == [] or launcher.mod.jobs == []
            assert h.held_slots() == 0
        finally:
            await h.close()
    asyncio.run(main())


def test_a_poll_response_stays_under_the_fetch_cap(tmp_path):
    """Jobs that do not fit one response wait for the next: three 1.5 MB jobs cannot ride
    together under the 4,000,000-byte budget."""
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            await h.ensure_started()
            rs = await asyncio.gather(*[h.submit(complete("z" * 1_500_000), soon(10))
                                        for _ in range(3)])
            assert all(r["ok"] for r in rs)
            sizes = [len(json.dumps(b)) for b in launcher.mod.responses]
            assert len(sizes) >= 2 and max(sizes) <= host_mod.POLL_BUDGET
        finally:
            await h.close()
    asyncio.run(main())


# ---- retirement ---------------------------------------------------------------------------


def test_host_exit_fails_every_in_flight_job_and_the_next_call_starts_a_new_one(tmp_path):
    async def main():
        launcher = FakeLauncher(handler=hold)
        h = make_host(tmp_path, launcher)
        try:
            calls = [asyncio.ensure_future(h.submit(agent_job(), None)) for _ in range(4)]
            await until(lambda: len(launcher.procs) == 1 and len(launcher.mod.jobs) == 4)
            launcher.procs[0].crash("killed by signal 9 (SIGKILL)")
            for c in calls:
                with pytest.raises(AgentFailed, match=r"agent host retired: killed by signal 9"):
                    await c
            assert h.held_slots() == 0
            launcher.plan.append({"handler": echo})
            r = await h.submit(complete("after"), soon(5))
            assert r["text"] == "echo:after" and len(launcher.procs) == 2
            assert [j["prompt"] for j in launcher.mod.jobs] == ["after"]    # nothing replayed
        finally:
            await h.close()
    asyncio.run(main())


def test_a_lapsed_lease_retires_the_generation(tmp_path):
    """A mod whose poll loop died: the process lives, the channel goes quiet."""
    async def stop_polling(mod, job):
        mod.polling = False                  # no further poll, and no result
        await asyncio.sleep(3600)

    async def main():
        launcher = FakeLauncher(handler=stop_polling, poll_wait_ms=100)
        h = make_host(tmp_path, launcher, lease_s=0.5)
        try:
            with pytest.raises(AgentFailed, match="agent host retired: lease"):
                await h.submit(agent_job(), None)
            assert launcher.procs[0].closed
        finally:
            await h.close()
    asyncio.run(main())


def test_a_lost_delivery_retires_the_generation(tmp_path):
    """A poll whose `ack` is older than the last response sent: that response was lost."""
    async def main():
        launcher = FakeLauncher(ack=False, handler=hold)
        h = make_host(tmp_path, launcher)
        try:
            with pytest.raises(AgentFailed, match="agent host retired: delivery"):
                await h.submit(agent_job(), None)
        finally:
            await h.close()
    asyncio.run(main())


def test_a_lost_response_carrying_only_cancels_retires_the_generation(tmp_path):
    async def main():
        launcher = FakeLauncher(handler=hold)
        h = make_host(tmp_path, launcher)
        try:
            other = asyncio.ensure_future(h.submit(agent_job("other"), None))
            await until(lambda: len(launcher.procs) == 1 and len(launcher.mod.jobs) == 1)
            short = asyncio.ensure_future(h.submit(agent_job("short"), soon(0.5)))
            await until(lambda: len(launcher.mod.jobs) == 2)
            await asyncio.sleep(0.1)         # the poll after it has acked it
            launcher.mod.ack = False         # from here on the mod's ack stays put
            with pytest.raises(TimeoutError):
                await short
            # the cancel goes out in a response of its own; the next poll's ack is stale
            with pytest.raises(AgentFailed, match="delivery"):
                await other
        finally:
            await h.close()
    asyncio.run(main())


def test_a_result_the_mod_cannot_deliver_ends_in_the_lease_retirement(tmp_path):
    """The mod's rule when its result POSTs all fail: it stops polling. The kernel never
    sees the result, so only the lease can end the call — and it does."""
    async def undeliverable(mod, job):
        mod.polling = False                  # and no POST lands: the handler returns None

    async def main():
        h = make_host(tmp_path, FakeLauncher(handler=undeliverable, poll_wait_ms=100),
                      lease_s=0.5)
        try:
            with pytest.raises(AgentFailed, match="lease"):
                await h.submit(complete(), None)
        finally:
            await h.close()
    asyncio.run(main())


def test_three_failures_within_the_window_switch_the_host_off(tmp_path, capsys):
    async def main():
        launcher = FakeLauncher(handler=hold)
        h = make_host(tmp_path, launcher)
        try:
            for n in range(3):
                call = asyncio.ensure_future(h.submit(agent_job(), None))
                await until(lambda n=n: len(launcher.procs) == n + 1 and launcher.mod.jobs)
                launcher.procs[n].crash(f"exit code {n + 1}")
                with pytest.raises(AgentFailed):
                    await call
            assert not h.enabled()
            with pytest.raises(HostUnavailable, match="PTC_HOST"):
                await h.ensure_started()
            assert len(launcher.procs) == 3
        finally:
            await h.close()
    asyncio.run(main())
    err = capsys.readouterr().err
    assert err.count("PTC_HOST") == 1 and "for the rest of this kernel" in err


def test_failed_starts_count_toward_the_crash_loop(tmp_path, capsys):
    async def main():
        launcher = FakeLauncher(protocol=7)
        h = make_host(tmp_path, launcher)
        try:
            for _ in range(3):
                with pytest.raises(HostUnavailable):
                    await h.ensure_started()
            assert not h.enabled()
        finally:
            await h.close()
    asyncio.run(main())
    err = capsys.readouterr().err
    assert err.count("did not start") == 3 and "for the rest of this kernel" in err


def test_a_start_failure_prints_one_warning(tmp_path, capsys):
    async def main():
        launcher = FakeLauncher(protocol=7)
        h = make_host(tmp_path, launcher)
        try:
            results = await asyncio.gather(*[h.ensure_started() for _ in range(5)],
                                           return_exceptions=True)
            assert all(isinstance(r, HostUnavailable) for r in results)
        finally:
            await h.close()
    asyncio.run(main())
    assert capsys.readouterr().err.count("did not start") == 1


def test_off_is_off(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher, enabled=False)
        assert not h.enabled()
        with pytest.raises(HostUnavailable, match="PTC_HOST=off"):
            await h.submit(complete(), soon(5))
        assert launcher.procs == []
    asyncio.run(main())


def test_close_fails_in_flight_jobs_and_stops_the_host(tmp_path):
    async def main():
        launcher = FakeLauncher(handler=hold)
        h = make_host(tmp_path, launcher)
        call = asyncio.ensure_future(h.submit(agent_job(), None))
        await until(lambda: launcher.procs and launcher.mod.jobs)
        await h.close()
        with pytest.raises(AgentFailed, match="agent host retired: closed"):
            await call
        assert launcher.procs[0].closed
        assert not (tmp_path / "host" / "g1.sock").exists()
    asyncio.run(main())


# ---- capacity and settlement ---------------------------------------------------------------


def test_caller_timeouts_do_not_release_slots_while_the_abandoned_jobs_run(tmp_path):
    """Settlement is separate from capacity: a timed-out caller is settled at once, but its
    slot is held until the job ends in the host, so abandoned agents cannot be joined by
    replacements past the cap."""
    async def main():
        launcher = FakeLauncher(handler=hold, honor_cancel=False)
        h = make_host(tmp_path, launcher, cap=3)
        try:
            for _ in range(2):
                with pytest.raises(TimeoutError):
                    await h.submit(agent_job(), soon(0.3))
            await until(lambda: len(launcher.mod.cancelled) == 2)
            assert h.held_slots() == 2
            # one slot left: a third job is admitted, a fourth waits on the cap
            third = asyncio.ensure_future(h.submit(agent_job("third"), None))
            await until(lambda: len(launcher.mod.jobs) == 3)
            with pytest.raises(TimeoutError):
                await h.submit(agent_job("fourth"), soon(0.3))
            assert len(launcher.mod.jobs) == 3          # the fourth never reached the host
            # an abandoned job ends: its late result releases its slot, exactly once
            first = launcher.mod.jobs[0]["id"]
            release(launcher.mod, first)
            await until(lambda: any(p[0] == first for p in launcher.mod.post_replies))
            [(_, status, body)] = [p for p in launcher.mod.post_replies if p[0] == first]
            assert status == 200 and body == {"ok": True, "late": True}
            assert h.held_slots() == 2
            assert await launcher.mod.post(first, {"ok": True, "text": "dup"}) == 404
            assert h.held_slots() == 2
            release(launcher.mod, launcher.mod.jobs[2]["id"])
            assert (await third)["text"] == "released"
        finally:
            await h.close()
    asyncio.run(main())


def test_a_late_result_racing_the_callers_timeout_releases_once(tmp_path):
    async def answer_at_the_deadline(mod, job):
        await asyncio.sleep(0.2)
        return {"ok": True, "text": "photo finish"}

    async def main():
        h = make_host(tmp_path, FakeLauncher(handler=answer_at_the_deadline), cap=4)
        try:
            await h.ensure_started()
            for _ in range(20):
                try:
                    await h.submit(complete(), soon(0.2))
                except TimeoutError:
                    pass
            await until(lambda: h.held_slots() == 0)
            assert h._sem._value == 4
        finally:
            await h.close()
    asyncio.run(main())


def test_a_cap_filled_by_abandoned_jobs_retires_the_generation(tmp_path):
    async def main():
        launcher = FakeLauncher(handler=hold, honor_cancel=False)
        h = make_host(tmp_path, launcher, cap=2)
        try:
            with pytest.raises(TimeoutError):
                await h.submit(agent_job(), soon(0.3))
            assert h.held_slots() == 1
            with pytest.raises(TimeoutError):
                await h.submit(agent_job(), soon(0.3))
            await until(lambda: launcher.procs[0].closed)
            assert h.held_slots() == 0 and h.retired_causes() == ["abandoned"]
            assert h.enabled()                   # `abandoned` is not a crash
        finally:
            await h.close()
    asyncio.run(main())


def test_a_cancelled_caller_queues_a_cancel(tmp_path):
    async def main():
        launcher = FakeLauncher(handler=hold)
        h = make_host(tmp_path, launcher)
        try:
            call = asyncio.ensure_future(h.submit(agent_job(), None))
            await until(lambda: launcher.procs and launcher.mod.jobs)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            jid = launcher.mod.jobs[0]["id"]
            await until(lambda: jid in launcher.mod.cancelled)
            await until(lambda: h.held_slots() == 0)          # the aborted result released it
            assert launcher.mod.post_replies[-1][2] == {"ok": True, "late": True}
        finally:
            await h.close()
    asyncio.run(main())


# ---- deadlines ----------------------------------------------------------------------------


def test_a_job_that_waited_for_a_permit_gets_only_the_remainder(tmp_path):
    async def main():
        launcher = FakeLauncher(handler=hold)
        h = make_host(tmp_path, launcher, cap=1)
        try:
            first = asyncio.ensure_future(h.submit(agent_job("first"), None))
            await until(lambda: launcher.procs and launcher.mod.jobs)
            second = asyncio.ensure_future(h.submit(complete("second"), soon(3.0)))
            await asyncio.sleep(2.0)
            release(launcher.mod, launcher.mod.jobs[0]["id"])
            await first
            await until(lambda: len(launcher.mod.jobs) == 2)
            tmo = launcher.mod.jobs[1]["timeoutMs"]
            assert 0 < tmo <= 1100, tmo
            release(launcher.mod, launcher.mod.jobs[1]["id"])
            await second
        finally:
            await h.close()
    asyncio.run(main())


def test_a_spent_deadline_raises_timeout_at_every_stage(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            with pytest.raises(TimeoutError):
                await h.submit(complete(), time.monotonic())        # before the start
            await h.ensure_started()
            with pytest.raises(TimeoutError):
                await h.submit(complete(), time.monotonic())        # started
            assert h.held_slots() == 0
        finally:
            await h.close()
    asyncio.run(main())


def test_a_job_with_no_deadline_has_no_timeout(tmp_path):
    async def main():
        launcher = FakeLauncher()
        h = make_host(tmp_path, launcher)
        try:
            await h.submit(complete(), None)
            assert launcher.mod.jobs[0]["timeoutMs"] is None
        finally:
            await h.close()
    asyncio.run(main())

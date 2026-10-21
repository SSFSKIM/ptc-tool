"""The agent host, live: acceptance 1, 2, 4 and 5 of the agent-host spec, each in a real
kernel driven from outside it, as a ptc cell runs.

Requires PTC_LIVE=1 and a `claude` CLI that can sign in (this machine's does, through the
local gateway). Every model call is haiku: `llm()` defaults to it, and every `agent.run`
here names it, since an agent call that names no model inherits the host's default model
(the session's `CLAUDE_CODE_SUBAGENT_MODEL` is emptied in the host, so it forces nothing).
Each test prints the model calls it made; the reader test counts them from transcripts.

    PTC_LIVE=1 uv run pytest test/live -q -rs -s -k host
"""
import json
import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from ptc.client import Completed, KernelClient, Running
from ptc.kernel import ensure_kernel, kill_kernel, restart_kernel
from ptc.ownership import read_owner
from ptc.paths import Config, kernel_dir

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")

LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "ptc-launch"
BUNDLED = "claude_agent_sdk/_bundled/claude"
#: The skill's host floors with about 10 % over what was measured (M4, CLI 2.1.305).
LLM_HOST_FLOOR = 40           # measured 34
READER_HOST_FLOOR = 3_700     # measured 3,367 outside git


@pytest.fixture
def host_kernel(ptc_home, monkeypatch):
    """A kernel that may start a host: `PTC_HOST` unset (the suite turns it off), and
    `PTC_LAUNCHER` this checkout's, as the plugin exports it in every real kernel."""
    assert "ANTHROPIC_API_KEY" not in os.environ      # billing stays on the subscription
    monkeypatch.delenv("PTC_HOST", raising=False)
    monkeypatch.setenv("PTC_LAUNCHER", str(LAUNCHER))
    keys = []

    def start(key: str, cwd: Path) -> KernelClient:
        ensure_kernel(key, cwd=str(cwd))
        keys.append(key)
        return KernelClient(key)
    yield start
    for k in keys:
        kill_kernel(k)


def children(pid: int) -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True,
                         text=True, check=True).stdout
    kids = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and int(parts[1]) == pid:
            kids.append((int(parts[0]), parts[2]))
    return kids


def kernel_pid(key: str) -> int:
    return read_owner(key).pid


def is_probe(cmd: str) -> bool:
    """The SDK's version check, `claude -v`, which every connect() runs first."""
    return cmd.rstrip().endswith(" -v")


def is_host(cmd: str) -> bool:
    return BUNDLED in cmd and re.search(r"--plugin-dir\S*\s+\S*host-plugin", cmd) is not None


def run_cell(kc: KernelClient, code: str, *, timeout_s: float, sample=None) -> Completed:
    """Run a cell, calling `sample()` every 0.2 s while it runs."""
    cfg = Config.from_env()
    out = kc.exec_cell(textwrap.dedent(code), timeout_s=1 if sample else timeout_s, config=cfg)
    end = time.monotonic() + timeout_s
    while isinstance(out, Running):
        if sample:
            sample()
        assert time.monotonic() < end, f"cell still running: {out.output[-2000:]}"
        out = kc.wait_cell(out.cell_id, timeout_s=0.2, since=out.next_offset) \
            if sample else kc.wait_cell(out.cell_id, timeout_s=timeout_s)
    assert isinstance(out, Completed), out
    return out


def full_output(key: str, out: Completed) -> str:
    """A cell's whole log: `run_cell`'s waits each return only what was new."""
    return (out.log_path or kernel_dir(key) / "cells" / f"{out.cell_id}.log").read_text()


@live
def test_a_64_wide_llm_fan_out_runs_on_one_host(host_kernel, tmp_path):
    """Acceptance 1: 64 correct replies; one host for the kernel and no other bundled CLI
    while it runs; then the same fan-out on the CLI route, at its cap of 8."""
    work = tmp_path / "a1"
    work.mkdir()
    kc = host_kernel("a1host", work)
    kpid = kernel_pid("a1host")
    seen_hosts: set[int] = set()
    seen_clis: set[int] = set()
    most_at_once = [0]

    def sample():
        kids = [(p, c) for p, c in children(kpid) if BUNDLED in c and not is_probe(c)]
        hosts = {p for p, c in kids if is_host(c)}
        seen_hosts.update(hosts)
        seen_clis.update(p for p, c in kids if not is_host(c))
        most_at_once[0] = max(most_at_once[0], len(hosts))

    fan_out = """
        import time
        t = time.time()
        rs = await asyncio.gather(*[llm(f"Reply with exactly the number {i}.", via=VIA)
                                    for i in range(64)])
        print("RESULT", VIA, sum(r.strip() == str(i) for i, r in enumerate(rs)),
              round(time.time() - t, 1))
    """
    out = run_cell(kc, fan_out.replace("VIA", '"auto"'), timeout_s=300, sample=sample)
    host_line = re.search(r"RESULT auto (\d+) ([\d.]+)", full_output("a1host", out))
    assert host_line, full_output("a1host", out)
    print(f"\nhost route: {host_line.group(1)}/64 right in {host_line.group(2)} s; "
          f"hosts seen {sorted(seen_hosts)}, CLIs seen {sorted(seen_clis)}")
    assert host_line.group(1) == "64"
    assert most_at_once[0] == 1 and len(seen_hosts) == 1
    assert seen_clis == set(), "a bundled CLI other than the host ran under this kernel"

    out = run_cell(kc, fan_out.replace("VIA", '"cli"'), timeout_s=600)
    cli_line = re.search(r"RESULT cli (\d+) ([\d.]+)", full_output("a1host", out))
    assert cli_line, full_output("a1host", out)
    print(f"cli route: {cli_line.group(1)}/64 right in {cli_line.group(2)} s (cap 8)")
    print("model calls: 128 (64 host completes, 64 CLI children)")
    assert cli_line.group(1) == "64"
    assert float(cli_line.group(2)) > float(host_line.group(2))


def _requests(path: Path) -> list[dict]:
    seen: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = d.get("message") or {}
        if d.get("type") == "assistant" and m.get("id") and m["id"] not in seen:
            u = m.get("usage") or {}
            seen[m["id"]] = {"model": m.get("model"), "tokens": sum(u.get(k) or 0 for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))}
    return list(seen.values())


@live
def test_32_reader_agents_run_on_the_host(host_kernel, tmp_path):
    """Acceptance 2: 32 readers in a git cwd, all right, each first request at most 4,200
    prompt tokens, every request on haiku though the session exports another subagent
    model."""
    repo = tmp_path / "a2repo"
    repo.mkdir()
    counts = {}
    for i in range(32):
        n = 3 + i
        # No trailing newline: after one, the Read tool shows an empty numbered line past
        # the last, and a reader counting rows is off by one (3 of 32 were, live). Lines are
        # labelled from 1, as Read numbers them: labelled from 0, the last line of a 29-line
        # file read `29\tline 28` and a haiku reader answered 28 (1 of 32, M4).
        (repo / f"f{i:02d}.txt").write_text("\n".join(f"line {k}" for k in range(1, n + 1)))
        counts[str(repo / f"f{i:02d}.txt")] = n
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "files"], check=True)
    kc = host_kernel("a2host", repo)
    out = run_cell(kc, f"""
        import json
        paths = {json.dumps(list(counts))}
        rs = await asyncio.gather(*[agent.run(
            f"How many lines does {{p}} have? Reply with the number only.",
            profile="reader", model="haiku") for p in paths])
        print("RESULT", json.dumps([[p, r.text.strip(), r.via, r.agent_id]
                                    for p, r in zip(paths, rs)]))
    """, timeout_s=600)
    rows = json.loads(re.search(r"RESULT (.*)", full_output("a2host", out)).group(1))
    # The whole stripped reply: the task names its reply format, so READER_SYSTEM's
    # citation must not ride along (7 of 32 replies added it before the prompt said so).
    wrong = [(p, ans, counts[p]) for p, ans, _, _ in rows if ans != str(counts[p])]
    assert all(via == "host" for _, _, via, _ in rows)
    time.sleep(3)              # the engine writes a subagent's last rows after its answer
    registry = {r["agent_id"]: r for r in json.loads(
        (kernel_dir("a2host") / "agents.json").read_text()) if r.get("agent_id")}
    firsts, calls, models = [], 0, set()
    for _, _, _, agent_id in rows:
        row = registry[agent_id]
        assert row["provider"] == "claude-host" and row["status"] == "done"
        reqs = _requests(Path(row["transcript"]))
        firsts.append(reqs[0]["tokens"])
        calls += len(reqs)
        models.update(r["model"] for r in reqs)
    print(f"\nreaders: {32 - len(wrong)}/32 right; first-request prompt tokens "
          f"min {min(firsts)} max {max(firsts)}; models {sorted(models)}; "
          f"model calls: {calls}")
    assert wrong == []
    assert max(firsts) <= 4200
    assert models and all("haiku" in m for m in models)



@live
def test_the_host_floors(host_kernel, tmp_path):
    """The host rows of the skill's floors table, outside git: an `llm()` completion (its
    usage, as the mod posts it) and a `reader` agent (its transcript's first request).
    Acceptance 2 gives the reader's figure in a git cwd. Three model calls at most."""
    work = tmp_path / "floors"
    work.mkdir()
    kc = host_kernel("floorshost", work)
    out = run_cell(kc, """
        import json
        from ptc.runtime import routes
        from ptc.runtime.host import get_host
        from ptc.runtime.profiles import LLM_SYSTEM
        res = await get_host().submit(routes.complete_job(
            "Reply with exactly: OK", system=LLM_SYSTEM, model="haiku", max_tokens=None,
            effort=None), None)
        print("LLM", res["text"].strip(), json.dumps(res["usage"]))
        r = await agent.run("Reply with exactly: OK", profile="reader", model="haiku")
        print("READER", r.text.strip(), r.via, r.agent_id)
    """, timeout_s=180)
    text = full_output("floorshost", out)
    reply, usage = re.search(r"LLM (\S+) (\{.*\})", text).groups()
    u = json.loads(usage)
    llm_tokens = sum(u.get(k) or 0 for k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    answer, via, agent_id = re.search(r"READER (\S+) (\S+) (\S+)", text).groups()
    time.sleep(3)              # the engine writes a subagent's last rows after its answer
    row = next(r for r in json.loads((kernel_dir("floorshost") / "agents.json").read_text())
               if r.get("agent_id") == agent_id)
    reqs = _requests(Path(row["transcript"]))
    print(f"\nhost llm(): {llm_tokens} prompt tokens ({u}); host reader outside git: "
          f"{reqs[0]['tokens']}; model calls: {1 + len(reqs)}")
    assert reply == "OK" and answer == "OK" and via == "host"
    assert 0 < llm_tokens <= LLM_HOST_FLOOR
    assert reqs[0]["tokens"] <= READER_HOST_FLOOR

@live
def test_a_host_crash_fails_its_jobs_and_a_new_host_takes_the_next_call(host_kernel, tmp_path):
    """Acceptance 4: `kill -9` the host while a 32-wide fan-out is in flight. Every
    in-flight call raises AgentFailed naming the host's exit; the next llm() succeeds on a
    new host pid."""
    work = tmp_path / "a4"
    work.mkdir()
    kc = host_kernel("a4host", work)
    out = run_cell(kc, """
        import subprocess
        from ptc.runtime.agents import AgentFailed
        from ptc.runtime.host import get_host
        await llm("Reply with exactly: OK")                     # the host is up
        host = get_host()
        old = host.pid
        calls = [asyncio.ensure_future(llm(f"Write about 300 words on the number {i}."))
                 for i in range(32)]
        while host.held_slots() < 32:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        in_flight = host.held_slots()
        subprocess.run(["kill", "-9", str(old)], check=True)
        rs = await asyncio.gather(*calls, return_exceptions=True)
        failed = [r for r in rs if isinstance(r, BaseException)]
        print("IN_FLIGHT", in_flight, "OK", len(rs) - len(failed), "FAILED", len(failed))
        print("KINDS", sorted({type(r).__name__ for r in failed}))
        print("MESSAGES", sorted({str(r) for r in failed}))
        print("HELD", host.held_slots())
        after = await llm("Reply with exactly: OK")
        print("AFTER", after.strip(), "OLD", old, "NEW", host.pid)
    """, timeout_s=300)
    text = full_output("a4host", out)
    print("\n" + text)
    in_flight, ok, failed = map(int, re.search(r"IN_FLIGHT (\d+) OK (\d+) FAILED (\d+)",
                                               text).groups())
    print(f"model calls: at most {2 + 32} (the warm-up, 32 cut or answered, the next call)")
    assert failed >= in_flight - ok and failed >= 1
    assert "KINDS ['AgentFailed']" in text
    messages = re.search(r"MESSAGES (.*)", text).group(1)
    assert "agent host retired: killed by signal 9 (SIGKILL)" in messages
    assert "HELD 0" in text
    after, old, new = re.search(r"AFTER (\S+) OLD (\d+) NEW (\d+)", text).groups()
    assert after == "OK" and old != new


@live
def test_no_host_outlives_its_kernel(host_kernel, tmp_path):
    """Acceptance 5: after a restart, the old host pid is gone."""
    work = tmp_path / "a5"
    work.mkdir()
    kc = host_kernel("a5host", work)
    out = run_cell(kc, """
        from ptc.runtime.host import get_host
        print("REPLY", (await llm("Reply with exactly: OK")).strip(), "PID", get_host().pid)
    """, timeout_s=120)
    reply, pid = re.search(r"REPLY (\S+) PID (\d+)", full_output("a5host", out)).groups()
    pid = int(pid)
    assert reply == "OK"
    assert subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0
    restart_kernel("a5host")
    deadline = time.monotonic() + 10
    while subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0:
        assert time.monotonic() < deadline, f"host {pid} outlived its kernel's restart"
        time.sleep(0.1)
    print(f"\nhost {pid} gone after the restart; model calls: 1")


@live
def test_llm_answers_on_the_cli_with_the_host_off_or_unloadable(ptc_home, tmp_path,
                                                                monkeypatch):
    """Acceptance 6: with PTC_HOST=off, and separately with the host plugin unloadable (the
    launcher's plugin root holding an empty `host-plugin/`), `llm()` still answers on the
    CLI route; the unloadable case prints one warning in the cell output."""
    assert "ANTHROPIC_API_KEY" not in os.environ
    work = tmp_path / "a6"
    work.mkdir()
    fake_root = tmp_path / "fake-plugin-root"
    (fake_root / "bin").mkdir(parents=True)
    (fake_root / "host-plugin").mkdir()
    cell = """
        r = await llm("Reply with exactly: OK")
        print("REPLY", r.strip())
    """
    outputs = {}
    for key, env in (("a6off", {"PTC_HOST": "off", "PTC_LAUNCHER": str(LAUNCHER)}),
                     ("a6gone", {"PTC_LAUNCHER": str(fake_root / "bin" / "ptc-launch")})):
        monkeypatch.delenv("PTC_HOST", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        ensure_kernel(key, cwd=str(work))
        try:
            out = run_cell(KernelClient(key), cell, timeout_s=120)
            outputs[key] = full_output(key, out)
            audit = [json.loads(x) for x in
                     (kernel_dir(key) / "audit.jsonl").read_text().splitlines()]
            assert [a["via"] for a in audit if a["kind"] == "llm"] == ["cli"]
        finally:
            kill_kernel(key)
    print("\n" + "\n".join(f"{k}: {v!r}" for k, v in outputs.items()))
    print("model calls: 2 (CLI children)")
    assert "REPLY OK" in outputs["a6off"] and "agent host" not in outputs["a6off"]
    assert "REPLY OK" in outputs["a6gone"]
    assert outputs["a6gone"].count("ptc: the agent host did not start") == 1
    assert "no host plugin at" in outputs["a6gone"]

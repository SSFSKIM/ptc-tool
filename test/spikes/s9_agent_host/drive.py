"""S9 driver: answer M1's questions (Q1–Q9) against a real agent host.

    uv run python test/spikes/s9_agent_host/drive.py --q 1            # one question
    uv run python test/spikes/s9_agent_host/drive.py --ramp 8,32,128,256
    uv run python test/spikes/s9_agent_host/drive.py --schema 100     # Q6
    uv run python test/spikes/s9_agent_host/drive.py --q 4 --dry-run  # projection only

Every model call is haiku: jobs name `haiku`, lean types name `haiku`, the host's main
loop runs `haiku`, and the session variables that would force another model on
subagents are stripped (hostproc.STRIP). Each mode prints the calls it made, counted from
the jobs it sent and each agent's transcript (one request per assistant message id), and
appends them to `out/ledger.jsonl`. A mode whose projection would take the ledger past
`--budget` (850, M1's cap) refuses to run.

Each run writes `out/<mode>-<stamp>/`: the host's debug log, stderr, SDK messages, and
`report.json`, the evidence the spec's Surprises & Discoveries cite.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from channel import StubChannel
from hostproc import (
    MODEL,
    Sampler,
    requests_of,
    sanitize_environ,
    start_host,
    transcript,
)

OUT = HERE / "out"
LEDGER = OUT / "ledger.jsonl"
BUDGET = 850

READER_SYSTEM = ("You are a read-only investigator. Use only the tools you are given. Cite "
                 "absolute paths. Be brief, and end with your findings.")
BARE_SYSTEM = "Answer directly and concisely. No preamble."


def reader_type(max_turns: int = 50) -> dict:
    return {"prompt": READER_SYSTEM, "tools": ["Read", "Grep", "Glob"], "model": MODEL,
            "effort": None, "permissionMode": "bypassPermissions", "maxTurns": max_turns}


def complete_job(prompt: str, *, system: str = BARE_SYSTEM, max_tokens: int = 256,
                 timeout_ms: int = 120_000) -> dict:
    return {"id": f"j-{uuid.uuid4().hex}", "kind": "complete", "model": MODEL, "system": system,
            "prompt": prompt, "promptFile": None, "systemFile": None, "maxTokens": max_tokens,
            "effort": None, "timeoutMs": timeout_ms}


def agent_job(prompt: str, *, type_: dict | None = None, cwd: str | None = None,
              timeout_ms: int | None = 600_000, cancel_via: str | None = None) -> dict:
    job = {"id": f"j-{uuid.uuid4().hex}", "kind": "agent", "type": type_ or reader_type(),
           "prompt": prompt, "promptFile": None, "cwd": cwd, "description": "ptc agent",
           "timeoutMs": timeout_ms}
    if cancel_via is not None:
        job["cancelVia"] = cancel_via
    return job


# ---- the ledger ---------------------------------------------------------------------------


def spent() -> int:
    if not LEDGER.exists():
        return 0
    return sum(json.loads(line)["calls"] for line in LEDGER.read_text().splitlines() if line)


def record(mode: str, calls: int, detail: dict) -> None:
    OUT.mkdir(exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode,
                            "calls": calls, **detail}) + "\n")


class Session:
    """One mode's run: its directory, its channel, its hosts, and its call count."""

    def __init__(self, mode: str, args):
        self.mode, self.args = mode, args
        self.dir = OUT / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.dir.mkdir(parents=True)
        self.work = self.dir / "work"
        self.work.mkdir()
        self.complete_calls = 0
        self.agent_calls = 0
        self.main_calls = 0
        self.uncounted_agents = 0
        self.models: set[str] = set()
        self.report: dict = {"mode": mode}
        self.chan: StubChannel | None = None
        self.host = None

    async def open_channel(self) -> StubChannel:
        if self.chan is not None:
            await self.chan.close()
        self.chan = StubChannel(self.dir)
        await self.chan.start()
        return self.chan

    async def start(self, *, main_tools: str | None = None, cap: int | None = None,
                    first_query: str | None = None, hello_timeout: float = 30.0):
        await self.open_channel()
        self.host = await start_host(
            self.chan, cwd=self.work, run_dir=self.dir, first_query=first_query,
            main_tools=main_tools or self.args.main_tools,
            subagent_cap=cap or self.args.subagent_cap or 64, hello_timeout=hello_timeout)
        return self.host

    async def stop(self):
        if self.host is not None:
            await self.host.close()
            self.host = None
        if self.chan is not None:
            await self.chan.close()
            self.chan = None

    def hello(self) -> dict:
        return self.chan.hello.result()

    async def run_jobs(self, jobs: list[dict]) -> list[dict]:
        """Submit all at once; return one row per job with its result and timings."""
        futs = []
        for j in jobs:
            if j["kind"] == "complete":
                self.complete_calls += 1
            futs.append(self.chan.submit(j))
        results = await asyncio.gather(*futs, return_exceptions=True)
        rows = []
        for j, r in zip(jobs, results):
            rec = self.chan.jobs[j["id"]]
            res = r if isinstance(r, dict) else {"ok": False, "reason": "driver", "detail": repr(r)}
            rows.append({"job": j, "result": res,
                         "latency": None if rec.result_at is None else rec.result_at - rec.submitted})
        for row in rows:
            if row["job"]["kind"] == "agent":
                self.count_agent(row)
        return rows

    def count_agent(self, row: dict) -> None:
        agent_id = row["result"].get("agentId")
        sid = self.hello()["spike"]["sessionId"]
        path = transcript(sid, agent_id) if agent_id else None
        if path is None:
            if agent_id or row["result"].get("reason") not in ("denied",):
                self.uncounted_agents += 1
            row["requests"] = None
            return
        reqs = requests_of(path)
        row["requests"] = reqs
        row["transcript"] = str(path)
        self.agent_calls += len(reqs)
        self.models |= {r["model"] for r in reqs if r["model"]}

    @property
    def calls(self) -> int:
        # An agent whose transcript was not found is counted at 2 requests (reader shape).
        return self.complete_calls + self.agent_calls + self.main_calls + 2 * self.uncounted_agents

    def finish(self) -> None:
        self.report["calls"] = {"total": self.calls, "complete": self.complete_calls,
                                "agent_requests": self.agent_calls, "main_loop": self.main_calls,
                                "agents_uncounted": self.uncounted_agents}
        self.report["agent_models"] = sorted(self.models)
        (self.dir / "report.json").write_text(json.dumps(self.report, indent=2, default=str))
        record(self.mode, self.calls, {"dir": str(self.dir)})
        non_haiku = [m for m in self.models if "haiku" not in m]
        print(f"calls={self.calls} (complete {self.complete_calls}, agent requests "
              f"{self.agent_calls}, main loop {self.main_calls}, uncounted agents "
              f"{self.uncounted_agents}) ledger={spent()}/{self.args.budget} "
              f"agent_models={sorted(self.models)} report={self.dir / 'report.json'}")
        if non_haiku:
            print(f"WARNING: non-haiku agent models: {non_haiku}")


def fmt_s(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]


def overlap(rows: list[dict]) -> int:
    """The most results whose mod-side [t0, t1] intervals overlap at once."""
    ev = []
    for r in rows:
        t0, t1 = r["result"].get("t0"), r["result"].get("t1")
        if t0 is not None and t1 is not None:
            ev += [(t0, 1), (t1, -1)]
    cur = best = 0
    for _, d in sorted(ev, key=lambda e: (e[0], e[1])):
        cur += d
        best = max(best, cur)
    return best


def write_files(root: Path, n: int, tag: str) -> list[tuple[Path, str]]:
    """n files whose second line holds a token only that file has."""
    out = []
    for i in range(n):
        tok = f"{tag}-{i}-{uuid.uuid4().hex[:8]}"
        p = root / f"{tag}-{i}.txt"
        p.write_text(f"file {i}\nTOKEN {tok}\nend\n")
        out.append((p, tok))
    return out


def reasons(rows: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        if not r["result"].get("ok"):
            k = r["result"].get("reason", "?")
            out[k] = out.get(k, 0) + 1
    return out


# ---- the questions ------------------------------------------------------------------------


async def q1(s: Session):
    """Does the mod start with no first query? If not, what is the smallest one?"""
    attempts = []
    for first in (None, "/context", "Reply with exactly: OK"):
        try:
            host = await s.start(first_query=first, hello_timeout=30)
            attempts.append({"first_query": first, "hello": True, "start_s": host.start_s,
                             "hello_body": s.hello()})
            if first is not None and not first.startswith("/"):
                s.main_calls += 1
            await s.stop()
            break
        except TimeoutError:
            attempts.append({"first_query": first, "hello": False})
            if first is not None and not first.startswith("/"):
                s.main_calls += 1
            await s.stop()
    s.report["attempts"] = attempts
    for a in attempts:
        print(f"first_query={a['first_query']!r} hello={a['hello']} start_s={fmt_s(a.get('start_s'))}")
    if attempts[-1]["hello"]:
        print("hello:", json.dumps(attempts[-1]["hello_body"]))


async def q2(s: Session):
    """Lean agents with no main-loop turn; does the type's tool list hold per main-tools setting?"""
    files = write_files(s.work, 1, "q2")
    (path, tok), = files
    rows_all = {}
    for setting in s.args.q2_settings.split(";"):
        await s.start(main_tools=setting)
        jobs = [agent_job(f"Use the Read tool on {path} and reply with the second line only.")]
        if setting == s.args.q2_settings.split(";")[0]:
            floor_type = {**reader_type(), "tools": []}
            jobs.append(agent_job("Reply with exactly: OK", type_=floor_type))
        rows = await s.run_jobs(jobs)
        main_tools = s.hello()["spike"]["mainTools"]
        await s.stop()
        out = []
        for r in rows:
            reqs = r.get("requests") or []
            out.append({"tools": r["job"]["type"]["tools"], "ok": r["result"].get("ok"),
                        "right": tok in (r["result"].get("text") or "") or
                                 (r["job"]["type"]["tools"] == [] and "OK" in (r["result"].get("text") or "")),
                        "reason": r["result"].get("reason"), "detail": r["result"].get("detail"),
                        "requests": len(reqs),
                        "first_prompt_tokens": reqs[0]["prompt_tokens"] if reqs else None,
                        "models": sorted({q["model"] for q in reqs}),
                        "transcript": r.get("transcript")})
        rows_all[setting] = {"main_tools": main_tools, "agents": out}
        for o in out:
            print(f"main_tools={setting} ({len(main_tools)} listed) type_tools={o['tools']} "
                  f"ok={o['ok']} right={o['right']} requests={o['requests']} "
                  f"first_prompt_tokens={o['first_prompt_tokens']} models={o['models']}"
                  + (f" reason={o['reason']} detail={str(o['detail'])[:200]}" if not o["ok"] else ""))
    s.report["settings"] = rows_all


async def q3(s: Session):
    """Does $.model.complete run N calls concurrently?"""
    n = s.args.n or 16
    await s.start()
    t = time.monotonic()
    rows = await s.run_jobs([complete_job(f"Reply with exactly the number {i}.") for i in range(n)])
    wall = time.monotonic() - t
    await s.stop()
    lat = [r["latency"] for r in rows if r["latency"] is not None]
    right = sum((r["result"].get("text") or "").strip() == str(i) for i, r in enumerate(rows))
    s.report.update(n=n, wall=wall, right=right, overlap=overlap(rows), reasons=reasons(rows),
                    latencies=lat, sum_latency=sum(lat))
    print(f"n={n} right={right} wall={wall:.2f} sum_latency={sum(lat):.2f} "
          f"max_overlap={overlap(rows)} reasons={reasons(rows)}")


async def ramp(s: Session, widths: list[int]):
    """Q4: half complete, half reader agents doing one Read, per width."""
    steps = []
    for w in widths:
        n_agents = w // 2
        n_complete = w - n_agents
        cap = s.args.subagent_cap or (w + 8)
        files = write_files(s.work, n_agents, f"w{w}")
        host = await s.start(cap=cap)
        jobs = [complete_job(f"Reply with exactly the number {i}.") for i in range(n_complete)]
        jobs += [agent_job(f"Use the Read tool on {p} and reply with the token on its second "
                           f"line only.") for p, _ in files]
        before = (s.complete_calls, s.agent_calls, s.uncounted_agents)
        with Sampler(host.pid) as smp:
            t = time.monotonic()
            rows = await s.run_jobs(jobs)
            wall = time.monotonic() - t
        await s.stop()
        ok = 0
        for i, r in enumerate(rows):
            text = r["result"].get("text") or ""
            if r["job"]["kind"] == "complete":
                good = r["result"].get("ok") and text.strip() == str(i)
            else:
                good = r["result"].get("ok") and files[i - n_complete][1] in text
            r["right"] = bool(good)
            ok += bool(good)
        lat = [r["latency"] for r in rows if r["latency"] is not None]
        fail = w - ok
        wrong = sum(1 for r in rows if r["result"].get("ok") and not r["right"])
        step_calls = (s.complete_calls - before[0]) + (s.agent_calls - before[1]) \
            + 2 * (s.uncounted_agents - before[2])
        step = {"width": w, "ok": ok, "fail": fail, "wrong_answers": wrong, "wall": wall,
                "rss_peak_mb": smp.peak_rss_mb, "cpu_peak_pct": smp.peak_cpu_pct,
                "cpu_s": smp.cpu_s, "p50": pct(lat, 0.5), "p95": pct(lat, 0.95),
                "reasons": reasons(rows), "subagent_cap": cap, "calls": step_calls,
                "max_overlap_complete": overlap([r for r in rows if r["job"]["kind"] == "complete"]),
                "max_overlap_agent": overlap([r for r in rows if r["job"]["kind"] == "agent"]),
                "denied_details": sorted({str(r["result"].get("detail"))[:200] for r in rows
                                          if r["result"].get("reason") == "denied"})}
        steps.append(step)
        print(f"width={w} ok={ok} fail={fail} wall={wall:.2f} rss_peak={smp.peak_rss_mb:.0f} "
              f"p50={fmt_s(step['p50'])} p95={fmt_s(step['p95'])} cpu_s={fmt_s(smp.cpu_s)} "
              f"cpu_peak={smp.peak_cpu_pct:.0f}% cap={cap} calls={step_calls} "
              f"overlap_c={step['max_overlap_complete']} overlap_a={step['max_overlap_agent']} "
              f"wrong={wrong} reasons={step['reasons']}", flush=True)
        if step["denied_details"]:
            print("  denied:", step["denied_details"])
    s.report["steps"] = steps


async def q5(s: Session):
    """Can a running subagent be stopped? Does aborting $.model.complete resolve `aborted`?"""
    out = {}
    await s.start()
    j = complete_job("Count from 1 to 400, one number per line, nothing else.", max_tokens=4096)
    s.complete_calls += 1
    fut = s.chan.submit(j)
    await asyncio.sleep(s.args.cancel_after)
    t = time.monotonic()
    s.chan.abandon(j["id"])
    try:
        await fut
    except TimeoutError:
        pass
    rec = s.chan.jobs[j["id"]]
    while not rec.released and time.monotonic() - t < 60:
        await asyncio.sleep(0.1)
    late = rec.job.get("_late_result") or {}
    out["complete"] = {"released": rec.released, "late_result": late,
                       "cancel_to_result_s": None if rec.result_at is None else rec.result_at - t}
    print(f"complete: released={rec.released} reason={late.get('reason')} "
          f"cancel_to_result_s={fmt_s(out['complete']['cancel_to_result_s'])}")
    files = write_files(s.work, 8, "q5")
    listing = " ".join(str(p) for p, _ in files)
    for via in s.args.cancel_vias.split(","):
        job = agent_job(f"Read each of these files one at a time, one Read call per turn, in "
                        f"order: {listing}. Then reply with how many you read.",
                        type_=reader_type(max_turns=10), cancel_via=via)
        fut = s.chan.submit(job)
        await asyncio.sleep(s.args.cancel_after)
        t = time.monotonic()
        s.chan.abandon(job["id"])
        try:
            await fut
        except TimeoutError:
            pass
        rec = s.chan.jobs[job["id"]]
        while not rec.released and time.monotonic() - t < 180:
            await asyncio.sleep(0.2)
        late = rec.job.get("_late_result") or {}
        row = {"job": job, "result": late}
        s.count_agent(row)
        reqs = row.get("requests") or []
        out[via] = {"released": rec.released, "reason": late.get("reason"),
                    "detail": str(late.get("detail"))[:300], "text": str(late.get("text"))[:300],
                    "cancel_to_result_s": None if rec.result_at is None else rec.result_at - t,
                    "requests": len(reqs), "transcript": row.get("transcript")}
        print(f"agent cancel_via={via}: released={rec.released} reason={late.get('reason')} "
              f"requests={len(reqs)} cancel_to_result_s={fmt_s(out[via]['cancel_to_result_s'])} "
              f"text={out[via]['text'][:80]!r}")
    out["main_tools"] = s.hello()["spike"]["mainTools"]
    await s.stop()
    s.report["cancel"] = out


SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"},
                                           "parity": {"enum": ["even", "odd"]}},
          "required": ["n", "parity"], "additionalProperties": False}


def parse_reply(text: str):
    t = text.strip()
    m = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    return json.loads(t)


async def q6(s: Session, n: int):
    """Prompt + validate structured output over n host completes."""
    import jsonschema
    system = (BARE_SYSTEM + "\n\nReply with one JSON value that validates against this JSON "
              "Schema, and nothing else:\n" + json.dumps(SCHEMA))
    await s.start()
    prompts = [f"Give the number {i} and its parity." for i in range(n)]

    def check(text: str | None, i: int):
        try:
            v = parse_reply(text or "")
            jsonschema.validate(v, SCHEMA)
            return None if v["n"] == i else f"wrong n: {v['n']}"
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {str(e)[:200]}"

    rows = await s.run_jobs([complete_job(p, system=system) for p in prompts])
    errs = {i: (check(r["result"].get("text"), i) if r["result"].get("ok")
                else f"call failed: {r['result'].get('reason')}") for i, r in enumerate(rows)}
    first_fail = {i: e for i, e in errs.items() if e}
    retry_rows = []
    if first_fail:
        if spent() + s.calls + len(first_fail) > s.args.budget:
            print(f"retries skipped: {len(first_fail)} would pass the budget")
        else:
            retry_rows = await s.run_jobs([complete_job(
                prompts[i] + f"\n\nYour previous reply did not validate: {e}\nReply again.",
                system=system) for i, e in first_fail.items()])
    final_fail = {}
    for (i, _), r in zip(first_fail.items(), retry_rows):
        e = check(r["result"].get("text"), i) if r["result"].get("ok") else "call failed"
        if e:
            final_fail[i] = {"error": e, "reply": r["result"].get("text")}
    await s.stop()
    s.report.update(n=n, needed_retry=len(first_fail), failed_retry=len(final_fail),
                    first_errors=first_fail, final=final_fail,
                    sample_replies=[r["result"].get("text") for r in rows[:5]])
    print(f"n={n} needed_retry={len(first_fail)} failed_retry={len(final_fail)}")
    for i, e in list(first_fail.items())[:10]:
        print(f"  first failure {i}: {e}")


async def q7(s: Session):
    """$.http.fetch's body caps and a held request near 30 s, through probe jobs."""
    await s.start()
    probes = {
        "post-size": [1_000_000, 4_000_000, 4_194_304, 4_194_305, 8_000_000],
        "get-size": [1_000_000, 4_194_304, 4_194_305, 8_000_000, 32_000_000],
        "hold": [25_000, 29_000, 29_900, 30_100, 31_000, 40_000, 65_000],
    }
    out = {}
    for name, values in probes.items():
        job = {"id": f"j-{uuid.uuid4().hex}", "kind": "probe", "probe": name, "values": values}
        res = await s.chan.submit(job)
        out[name] = json.loads(res["text"]) if res.get("ok") else res
        for row in out[name] if isinstance(out[name], list) else [out[name]]:
            print(f"{name}: {json.dumps(row)[:300]}")
    s.report["probes"] = out
    s.report["max_poll_response_chars"] = s.chan.stats.max_response_chars
    await s.stop()


async def q8(s: Session):
    """What the host's death looks like to the driver, and how long a fresh start takes."""
    host = await s.start()
    first_start = host.start_s
    jobs = [complete_job("Count from 1 to 300, one number per line.", max_tokens=4096)
            for _ in range(2)]
    s.complete_calls += len(jobs)
    futs = [s.chan.submit(j) for j in jobs]
    await asyncio.sleep(1.5)
    polls_before = s.chan.stats.polls
    t_kill = time.monotonic()
    host.kill9()
    rc = await asyncio.wait_for(host.client._transport._process.wait(), 10)
    t_exit = time.monotonic()
    await asyncio.sleep(3)
    observed = {
        "exit_code": rc, "exit_after_s": t_exit - t_kill,
        "reader_error": repr(host.reader_error),
        "reader_error_after_s": None if host.reader_error_at is None else host.reader_error_at - t_kill,
        "jobs_pending": [not f.done() for f in futs],
        "polls_after_kill": s.chan.stats.polls - polls_before,
        "last_poll_before_kill_s": t_kill - (s.chan.stats.last_poll or t_kill),
    }
    try:
        await asyncio.wait_for(host.client.query("hello"), 5)
        observed["query_after_death"] = "accepted"
    except BaseException as e:  # noqa: BLE001
        observed["query_after_death"] = repr(e)[:300]
    for f in futs:
        f.cancel()
    await s.stop()
    t = time.monotonic()
    host2 = await s.start()
    observed["fresh_start_s"] = host2.start_s
    observed["fresh_connect_to_hello_wall_s"] = time.monotonic() - t
    observed["first_start_s"] = first_start
    rows = await s.run_jobs([complete_job("Reply with exactly: OK")])
    observed["after_restart"] = rows[0]["result"].get("text")
    await s.stop()
    s.report["death"] = observed
    print(json.dumps(observed, indent=2, default=str))


async def q9(s: Session):
    """Does a spawn's cwd set the subagent's working directory?"""
    dirs = {}
    for tag in ("a", "b"):
        d = s.work / f"dir-{tag}"
        d.mkdir()
        (d / f"marker-{tag}.txt").write_text(f"marker {tag}\n")
        (d / "same.txt").write_text(f"content of {tag}: {uuid.uuid4().hex[:8]}\n")
        dirs[tag] = d
    await s.start()
    prompt = ("List the files in your current working directory (Glob with pattern '*', no "
              "path), then Read same.txt by its relative name. Reply with the file names and "
              "same.txt's content.")
    rows = await s.run_jobs([agent_job(prompt, cwd=str(dirs[t])) for t in ("a", "b")])
    await s.stop()
    out = {}
    for tag, r in zip(("a", "b"), rows):
        text = r["result"].get("text") or ""
        other = "b" if tag == "a" else "a"
        content = (dirs[tag] / "same.txt").read_text().strip()
        reqs = r.get("requests") or []
        out[tag] = {"ok": r["result"].get("ok"), "own_marker": f"marker-{tag}" in text,
                    "other_marker": f"marker-{other}" in text, "own_content": content in text,
                    "transcript_cwds": sorted({q["cwd"] for q in reqs if q["cwd"]}),
                    "text": text[:400], "reason": r["result"].get("reason"),
                    "detail": str(r["result"].get("detail"))[:300]}
        print(f"cwd=dir-{tag}: ok={out[tag]['ok']} own_marker={out[tag]['own_marker']} "
              f"other_marker={out[tag]['other_marker']} own_content={out[tag]['own_content']} "
              f"transcript_cwds={out[tag]['transcript_cwds']}")
    s.report["cwd"] = out


# ---- entry --------------------------------------------------------------------------------


def projection(args) -> tuple[str, int]:
    if args.ramp:
        widths = [int(w) for w in args.ramp.split(",")]
        return f"ramp-{'-'.join(map(str, widths))}", sum(w - w // 2 + 2 * (w // 2) for w in widths)
    if args.schema:
        return "q6", args.schema
    q = args.q
    n = args.n or 16
    return f"q{q}", {1: 1, 2: 2 * len(args.q2_settings.split(";")) + 1, 3: n,
                     5: 1 + 10 * len(args.cancel_vias.split(",")), 7: 0, 8: 3, 9: 6}[q]


async def amain(args) -> int:
    mode, projected = projection(args)
    have = spent()
    print(f"mode={mode} projected_calls={projected} ledger={have}/{args.budget}")
    if args.dry_run:
        return 0
    if have + projected > args.budget:
        print(f"REFUSED: {have} + {projected} would pass the budget of {args.budget}")
        return 2
    gone = sanitize_environ()
    print(f"stripped from the host's environment: {gone}")
    s = Session(mode, args)
    try:
        if args.ramp:
            await ramp(s, [int(w) for w in args.ramp.split(",")])
        elif args.schema:
            await q6(s, args.schema)
        else:
            await {1: q1, 2: q2, 3: q3, 5: q5, 7: q7, 8: q8, 9: q9}[args.q](s)
    finally:
        await s.stop()
        s.finish()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--q", type=int, choices=[1, 2, 3, 4, 5, 7, 8, 9])
    ap.add_argument("--ramp", help="Q4: widths, e.g. 8,32,128,256")
    ap.add_argument("--schema", type=int, help="Q6: number of schema calls")
    ap.add_argument("--n", type=int, help="Q3: number of concurrent completes (16)")
    ap.add_argument("--main-tools", default="none",
                    help="the host main loop's tools: none | default | a comma list (none)")
    ap.add_argument("--q2-settings", default="none;default;Read,Grep,Glob",
                    help="Q2: main-loop tool settings to try, ';'-separated")
    ap.add_argument("--subagent-cap", type=int,
                    help="CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS (default: width + 8, else 64)")
    ap.add_argument("--cancel-vias", default="taskstop,step,tooldeny")
    ap.add_argument("--cancel-after", type=float, default=4.0)
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.q == 4 and not args.ramp:
        args.ramp = "8,32,128,256"
    if not (args.q or args.ramp or args.schema):
        ap.error("one of --q, --ramp, --schema")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())

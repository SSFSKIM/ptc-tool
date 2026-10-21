"""A fake agent host for `AgentHost`'s tests: the host plugin's mod, written in Python.

`FakeMod` speaks protocol 1 over the real channel socket, exactly as `host-plugin/hooks/
mods/host.tsx` does: `/hello`, then one long-poll loop acking each response's `seq`, every
job started without waiting for it, one result POST per job. Each behavior a test needs to
break is a knob: the protocol it claims, whether it acks, whether it keeps polling, how a
job runs (`handler`), and whether a cancel stops it.

`FakeLauncher` stands where the SDK launch stands in production: it is given the host's
environment and returns a `FakeProc` whose `wait()` resolves with the exit cause once the
test `crash()`es it, and whose `close()` stops the fake mod — the host never touches a pid.
"""
import asyncio
import json
from pathlib import Path

import httpx


class FakeMod:
    def __init__(self, socket: str, *, protocol: int = 1, ack: bool = True,
                 handler=None, honor_cancel: bool = True, poll_wait_ms: int = 25_000):
        self.socket = socket
        self.protocol = protocol
        self.ack = ack
        self.handler = handler or echo
        self.honor_cancel = honor_cancel
        self.poll_wait_ms = poll_wait_ms
        self.polling = True
        self.out_dir: str | None = None
        self.jobs: list[dict] = []           # every job received, in order
        self.cancelled: set[str] = set()
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.responses: list[dict] = []       # every non-empty poll response
        self.post_replies: list[tuple[str, int, dict]] = []
        self.hello_reply: tuple[int, dict] | None = None
        self._tasks: set = set()
        self._client: httpx.AsyncClient | None = None

    async def run(self) -> None:
        self._client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket),
                                         base_url="http://host", timeout=60)
        try:
            r = await self._client.post("/hello", json={"protocol": self.protocol,
                                                        "engine": "fake", "plugin": "fake",
                                                        "session": "host-session",
                                                        "cwd": "/fake/cwd"})
            self.hello_reply = (r.status_code, r.json())
            if r.status_code != 200:
                return
            self.out_dir = r.json().get("outDir")
            ack = 0
            while self.polling:
                try:
                    r = await self._client.get(
                        "/jobs", params={"wait": self.poll_wait_ms, "ack": ack})
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
                    continue
                if r.status_code != 200 or not self.polling:
                    return
                body = r.json()
                if body["jobs"] or body["cancel"]:
                    self.responses.append(body)
                if self.ack:                     # once False, the ack freezes where it is
                    ack = max(ack, body["seq"])
                for jid in body["cancel"]:
                    self.cancelled.add(jid)
                    if self.honor_cancel and jid in self.cancel_events:
                        self.cancel_events[jid].set()
                for job in body["jobs"]:
                    self.jobs.append(job)
                    self.cancel_events[job["id"]] = asyncio.Event()
                    t = asyncio.ensure_future(self._dispatch(job))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
        finally:
            await self._client.aclose()

    async def _dispatch(self, job: dict) -> None:
        try:
            result = await self.handler(self, job)
        except Exception as e:                      # noqa: BLE001 — the mod's own rule
            result = {"ok": False, "reason": "error", "detail": str(e)}
        if result is not None:
            await self.post(job["id"], result)

    def text_of(self, job: dict, field: str) -> str:
        """A job field as the mod reads it: inline, or from the file it was spilled to."""
        f = job.get(f"{field}File")
        return Path(f).read_text(encoding="utf-8") if f else job.get(field)

    async def post(self, job_id: str, result: dict) -> int:
        body = json.dumps(result)
        if len(body) > 3_500_000 and isinstance(result.get("text"), str):
            p = Path(self.out_dir) / f"{job_id}.txt"
            p.write_text(result["text"], encoding="utf-8")
            body = json.dumps({**result, "text": None, "textFile": str(p)})
        client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket),
                                   base_url="http://host", timeout=60)
        async with client:
            r = await client.post(f"/results/{job_id}", content=body,
                                  headers={"content-type": "application/json"})
        self.post_replies.append((job_id, r.status_code, r.json()))
        return r.status_code

    async def stop(self) -> None:
        self.polling = False
        for t in list(self._tasks):
            t.cancel()


def agent_ended(reason: str, answer: str, agent_id: str = "a1", usage=None) -> dict:
    """The Result the real mod posts when a subagent ends (`settleAgent`, host.tsx): kept
    in step with it by hand, so a fake failure carries its text exactly as the mod's does."""
    if reason == "answer":
        return {"ok": True, "text": answer, "usage": usage, "agentId": agent_id}
    return {"ok": False, "reason": reason, "detail": answer, "text": answer, "usage": usage,
            "agentId": agent_id}


async def echo(mod: FakeMod, job: dict) -> dict:
    """A `complete` answers its prompt back; an agent answers once cancelled or at once."""
    if job["kind"] == "complete":
        return {"ok": True, "text": f"echo:{mod.text_of(job, 'prompt')}",
                "usage": {"input_tokens": 1}}
    return {"ok": True, "text": f"agent:{mod.text_of(job, 'prompt')}", "agentId": "a1",
            "usage": None}


def held(mod: FakeMod, job: dict):
    """A handler that runs until its job is cancelled (if the mod honors cancels), then
    answers `aborted`; `release(mod, id)` ends it early with an answer."""
    async def run():
        await mod.cancel_events[job["id"]].wait()
        if job["id"] in mod.cancelled:
            return {"ok": False, "reason": "aborted", "detail": "stopped"}
        return {"ok": True, "text": "released", "agentId": "a-held"}
    return run()


async def hold(mod: FakeMod, job: dict):
    return await held(mod, job)


def release(mod: FakeMod, job_id: str) -> None:
    mod.cancel_events[job_id].set()


class FakeProc:
    def __init__(self, mod: FakeMod):
        self.mod = mod
        self.pid = 4242
        self._exit = asyncio.get_running_loop().create_future()
        self._task = asyncio.ensure_future(mod.run())
        self.closed = False

    async def wait(self) -> str:
        return await asyncio.shield(self._exit)

    def crash(self, cause: str = "killed by signal 9 (SIGKILL)") -> None:
        if not self._exit.done():
            self._exit.set_result(cause)
        self._task.cancel()

    async def close(self) -> None:
        self.closed = True
        await self.mod.stop()
        await asyncio.wait([self._task], timeout=0.2)    # a mod on its way out finishes
        self._task.cancel()
        if not self._exit.done():
            self._exit.set_result("closed")


class FakeLauncher:
    """`launch(env, cwd, plugin_dir)`: one FakeProc per generation, built by `make_mod`."""

    def __init__(self, **mod_kw):
        self.mod_kw = mod_kw
        self.procs: list[FakeProc] = []
        self.envs: list[dict] = []
        #: per-launch overrides, consumed in order: a dict of FakeMod kwargs, or an
        #: exception instance to raise from the launch itself.
        self.plan: list = []

    async def __call__(self, env: dict, cwd: str, plugin_dir) -> FakeProc:
        self.envs.append(env)
        kw = dict(self.mod_kw)
        if self.plan:
            step = self.plan.pop(0)
            if isinstance(step, BaseException):
                raise step
            kw.update(step)
        proc = FakeProc(FakeMod(env["PTC_HOST_SOCKET"], **kw))
        self.procs.append(proc)
        return proc

    @property
    def mod(self) -> FakeMod:
        return self.procs[-1].mod

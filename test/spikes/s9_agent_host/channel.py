"""S9's stub of the kernel's channel: HTTP/1.1 over a Unix socket, protocol 1.

What the spec's "The channel" describes, as far as the spike needs it: `/hello`, the
long-polled `/jobs` with `seq` acks, one `/results/<id>` per job (`late` once its caller
has been settled), cancels, the 4,000,000-character poll-response budget, and the
`textFile` spill of a large result. Not here (M3's): retirement, the lease, the crash
loop, the job-field spill to files. The spike only *records* a lost delivery (a stale
`ack`) and the time of the last poll, as evidence.

Spike-only routes, for Q7: `POST /probe/echo` answers the body length it received,
`GET /probe/big?n=` answers `n` characters, `GET /probe/hold?ms=` answers after `ms`.

The socket binds at a short path in the temp dir; `published` is a symlink to it under
the run directory, as `runtime/peek.py` does for the 104-byte `sun_path` cap. The mod is
handed the *short* path: `$.http.fetch`'s `socketPath` is a `sun_path` too.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROTOCOL = 1
#: The poll response's JSON is kept at or under this many characters ("Sizes").
POLL_BUDGET = 4_000_000
#: The longest hold the stub grants a poll, whatever `wait` asks.
MAX_HOLD_MS = 120_000


@dataclass
class JobRecord:
    job: dict
    future: asyncio.Future
    submitted: float                     # time.monotonic()
    delivered: float | None = None       # first poll response that carried it
    delivered_seq: int | None = None
    result_at: float | None = None
    late: bool = False                   # the caller was settled before the result came
    released: bool = False


@dataclass
class ChannelStats:
    polls: int = 0
    last_poll: float | None = None
    lost_deliveries: list = field(default_factory=list)   # (ack, last_sent_seq)
    late_results: int = 0
    unknown_results: int = 0
    max_response_chars: int = 0


class StubChannel:
    def __init__(self, run_dir: Path, *, protocol: int = PROTOCOL):
        self.run_dir = Path(run_dir)
        self.protocol = protocol
        self.out_dir = self.run_dir / "host" / "out"
        self.published = self.run_dir / "host.sock"
        self.path = str(Path(tempfile.gettempdir()) / (
            "ptc-host-" + hashlib.sha256(str(self.published).encode()).hexdigest()[:12] + ".sock"))
        self.jobs: dict[str, JobRecord] = {}
        self.queue: list[str] = []
        self.cancels: list[str] = []
        self.seq = 0
        self.stats = ChannelStats()
        self.hello: asyncio.Future | None = None
        self.hello_at: float | None = None
        self._wake: asyncio.Event | None = None
        self._server: asyncio.AbstractServer | None = None

    # ---- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.hello = loop.create_future()
        self._wake = asyncio.Event()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for p in (self.path, self.published):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        self._server = await asyncio.start_unix_server(self._serve, path=self.path)
        os.chmod(self.path, 0o600)
        self.published.symlink_to(self.path)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for p in (self.path, self.published):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        for rec in self.jobs.values():
            if not rec.future.done():
                rec.future.cancel()

    # ---- the kernel's side --------------------------------------------------------------

    def submit(self, job: dict) -> asyncio.Future:
        """Queue a job; its future resolves to the mod's Result dict."""
        job = dict(job)
        job.setdefault("id", f"j-{uuid.uuid4().hex}")
        if len(json.dumps(job)) > POLL_BUDGET:
            raise ValueError("job too large for one poll response (the spike does not spill)")
        fut = asyncio.get_running_loop().create_future()
        self.jobs[job["id"]] = JobRecord(job=job, future=fut, submitted=time.monotonic())
        self.queue.append(job["id"])
        self._wake.set()
        return fut

    def abandon(self, job_id: str) -> None:
        """Settle the caller now (as a deadline would) and queue a cancel for the job."""
        rec = self.jobs[job_id]
        rec.late = True
        if not rec.future.done():
            rec.future.set_exception(TimeoutError(f"{job_id} abandoned by the driver"))
            rec.future.exception()            # retrieved: no "never retrieved" warning
        self.cancels.append(job_id)
        self._wake.set()

    def in_flight(self) -> list[str]:
        return [i for i, r in self.jobs.items() if not r.released]

    # ---- HTTP ---------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                req = await _read_request(reader)
                if req is None:
                    break
                method, target, headers, body = req
                status, payload = await self._route(method, target, body)
                text = payload if isinstance(payload, str) else json.dumps(payload)
                data = text.encode()
                keep = headers.get("connection", "").lower() != "close"
                writer.write(
                    f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                    f"Connection: {'keep-alive' if keep else 'close'}\r\n\r\n".encode() + data)
                await writer.drain()
                if not keep:
                    break
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _route(self, method: str, target: str, body: bytes):
        url = urlsplit(target)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        if method == "POST" and path == "/hello":
            return self._on_hello(body)
        if method == "GET" and path == "/jobs":
            return await self._on_poll(int(q.get("wait", "0")), int(q.get("ack", "0")))
        if method == "POST" and path.startswith("/results/"):
            return self._on_result(path[len("/results/"):], body)
        if path == "/probe/echo":
            return 200, {"ok": True, "received": len(body.decode())}
        if path == "/probe/big":
            return 200, "x" * int(q["n"])
        if path == "/probe/hold":
            await asyncio.sleep(int(q["ms"]) / 1000)
            return 200, {"ok": True}
        return 404, {"ok": False, "reason": "no such route"}

    def _on_hello(self, body: bytes):
        hello = json.loads(body)
        if hello.get("protocol") != self.protocol:
            return 409, {"ok": False, "reason": "protocol"}
        self.hello_at = time.monotonic()
        if not self.hello.done():
            self.hello.set_result(hello)
        return 200, {"ok": True, "outDir": str(self.out_dir)}

    async def _on_poll(self, wait_ms: int, ack: int):
        now = time.monotonic()
        self.stats.polls += 1
        self.stats.last_poll = now
        if ack < self.seq:
            self.stats.lost_deliveries.append((ack, self.seq))
        deadline = now + min(wait_ms, MAX_HOLD_MS) / 1000
        while not (self.queue or self.cancels):
            left = deadline - time.monotonic()
            if left <= 0:
                return 200, {"seq": self.seq, "jobs": [], "cancel": []}
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), left)
            except TimeoutError:
                pass
        jobs, size = [], len('{"seq": 0000000000, "jobs": [], "cancel": []}')
        cancels, self.cancels = self.cancels, []
        size += sum(len(json.dumps(c)) + 2 for c in cancels)
        while self.queue:
            rec = self.jobs[self.queue[0]]
            enc = len(json.dumps(rec.job)) + 2
            if jobs and size + enc > POLL_BUDGET:
                break
            self.queue.pop(0)
            jobs.append(rec.job)
            size += enc
        self.seq += 1
        sent = time.monotonic()
        for j in jobs:
            rec = self.jobs[j["id"]]
            rec.delivered, rec.delivered_seq = sent, self.seq
        payload = {"seq": self.seq, "jobs": jobs, "cancel": cancels}
        self.stats.max_response_chars = max(self.stats.max_response_chars, len(json.dumps(payload)))
        return 200, payload

    def _on_result(self, job_id: str, body: bytes):
        rec = self.jobs.get(job_id)
        if rec is None or rec.released:
            self.stats.unknown_results += 1
            return 404, {"ok": False}
        result = json.loads(body)
        if result.get("textFile"):
            p = Path(result["textFile"])
            result["text"] = p.read_text()
            p.unlink()
        rec.result_at = time.monotonic()
        rec.released = True
        if rec.late:
            self.stats.late_results += 1
            rec.job["_late_result"] = result
            return 200, {"ok": True, "late": True}
        if not rec.future.done():
            rec.future.set_result(result)
        return 200, {"ok": True}


_REASONS = {200: "OK", 404: "Not Found", 409: "Conflict", 400: "Bad Request"}


async def _read_request(reader: asyncio.StreamReader):
    """One HTTP/1.1 request: (method, target, headers, body), or None at EOF."""
    line = await reader.readline()
    if not line:
        return None
    method, target, _ = line.decode("latin-1").split(" ", 2)
    headers: dict[str, str] = {}
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            n = int((await reader.readline()).strip().split(b";")[0], 16)
            if n == 0:
                await reader.readline()
                break
            body += await reader.readexactly(n)
            await reader.readline()
    else:
        body = await reader.readexactly(int(headers.get("content-length", "0")))
    return method, target, headers, body

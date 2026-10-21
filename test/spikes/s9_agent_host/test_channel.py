"""The stub channel against a fake mod: a Python client speaking protocol 1 over the socket.

Run:  uv run pytest test/spikes/s9_agent_host/test_channel.py -q
No host, no model calls.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from channel import POLL_BUDGET, StubChannel


async def http(path: str, method: str = "GET", target: str = "/", body=None):
    reader, writer = await asyncio.open_unix_connection(path)
    data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    writer.write(f"{method} {target} HTTP/1.1\r\nHost: host\r\nContent-Length: {len(data)}\r\n"
                 f"Connection: close\r\n\r\n".encode() + data)
    await writer.drain()
    status = int((await reader.readline()).split()[1])
    length = 0
    while (h := await reader.readline()) not in (b"\r\n", b""):
        k, _, v = h.decode().partition(":")
        if k.lower() == "content-length":
            length = int(v)
    payload = await reader.readexactly(length)
    writer.close()
    return status, json.loads(payload) if payload[:1] in (b"{", b"[") else payload.decode()


def run(coro):
    return asyncio.run(coro)


async def _with_channel(tmp_path, body):
    c = StubChannel(tmp_path)
    await c.start()
    try:
        await body(c)
    finally:
        await c.close()


def test_hello_and_protocol_mismatch(tmp_path):
    async def body(c):
        assert Path(c.path).name.startswith("ptc-host-") and c.published.is_symlink()
        st, r = await http(c.path, "POST", "/hello", {"protocol": 2, "engine": "x", "plugin": "y"})
        assert (st, r["reason"]) == (409, "protocol")
        assert not c.hello.done()
        st, r = await http(c.path, "POST", "/hello", {"protocol": 1, "engine": "x", "plugin": "y"})
        assert st == 200 and r["outDir"] == str(c.out_dir)
        assert (await c.hello)["engine"] == "x"
    run(_with_channel(tmp_path, body))


def test_empty_hold_returns_after_wait(tmp_path):
    async def body(c):
        t = time.monotonic()
        st, r = await http(c.path, target="/jobs?wait=200&ack=0")
        assert st == 200 and r == {"seq": 0, "jobs": [], "cancel": []}
        assert 0.18 <= time.monotonic() - t < 1.0
    run(_with_channel(tmp_path, body))


def test_held_poll_answers_at_once_on_a_job_then_result(tmp_path):
    async def body(c):
        poll = asyncio.create_task(http(c.path, target="/jobs?wait=5000&ack=0"))
        await asyncio.sleep(0.1)
        t = time.monotonic()
        fut = c.submit({"id": "j-1", "kind": "complete", "prompt": "hi"})
        st, r = await poll
        assert time.monotonic() - t < 0.5
        assert r["seq"] == 1 and [j["id"] for j in r["jobs"]] == ["j-1"]
        st, r = await http(c.path, "POST", "/results/j-1", {"ok": True, "text": "OK"})
        assert (st, r) == (200, {"ok": True})
        assert (await fut)["text"] == "OK"
        st, _ = await http(c.path, "POST", "/results/j-1", {"ok": True, "text": "again"})
        assert st == 404                     # released: a second result is unknown
        st, _ = await http(c.path, "POST", "/results/j-never", {"ok": True})
        assert st == 404
    run(_with_channel(tmp_path, body))


def test_abandon_cancels_and_a_late_result_releases(tmp_path):
    async def body(c):
        fut = c.submit({"id": "j-2", "kind": "agent"})
        _, r = await http(c.path, target="/jobs?wait=0&ack=0")
        assert r["seq"] == 1
        c.abandon("j-2")
        with pytest.raises(TimeoutError):
            await fut
        assert c.in_flight() == ["j-2"]      # the slot stays held until the result
        _, r = await http(c.path, target="/jobs?wait=1000&ack=1")
        assert r == {"seq": 2, "jobs": [], "cancel": ["j-2"]}
        st, r = await http(c.path, "POST", "/results/j-2", {"ok": False, "reason": "aborted"})
        assert (st, r) == (200, {"ok": True, "late": True})
        assert c.in_flight() == [] and c.stats.late_results == 1
    run(_with_channel(tmp_path, body))


def test_stale_ack_is_recorded_as_a_lost_delivery(tmp_path):
    async def body(c):
        c.submit({"id": "j-3", "kind": "complete"})
        await http(c.path, target="/jobs?wait=0&ack=0")
        assert c.stats.lost_deliveries == []
        await http(c.path, target="/jobs?wait=0&ack=0")   # never acked seq 1
        assert c.stats.lost_deliveries == [(0, 1)]
    run(_with_channel(tmp_path, body))


def test_result_text_file_is_read_and_deleted(tmp_path):
    async def body(c):
        fut = c.submit({"id": "j-4", "kind": "complete"})
        await http(c.path, target="/jobs?wait=0&ack=0")
        f = c.out_dir / "j-4.txt"
        f.write_text("\n" * 10)
        await http(c.path, "POST", "/results/j-4", {"ok": True, "text": None, "textFile": str(f)})
        assert (await fut)["text"] == "\n" * 10 and not f.exists()
    run(_with_channel(tmp_path, body))


def test_poll_responses_stay_under_the_budget(tmp_path):
    async def body(c):
        big = "x" * 1_500_000
        for i in range(4):
            c.submit({"id": f"j-b{i}", "kind": "complete", "prompt": big})
        seen, ack = [], 0
        while len(seen) < 4:
            _, r = await http(c.path, target=f"/jobs?wait=0&ack={ack}")
            assert len(json.dumps(r)) <= POLL_BUDGET
            seen += [j["id"] for j in r["jobs"]]
            ack = r["seq"]
        assert seen == [f"j-b{i}" for i in range(4)] and ack == 2
    run(_with_channel(tmp_path, body))


def test_probe_routes(tmp_path):
    async def body(c):
        st, r = await http(c.path, "POST", "/probe/echo", b"x" * 1000)
        assert r == {"ok": True, "received": 1000}
        _, r = await http(c.path, target="/probe/big?n=5000")
        assert len(r) == 5000
    run(_with_channel(tmp_path, body))

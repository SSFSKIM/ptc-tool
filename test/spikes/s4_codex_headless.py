"""S4: drive `codex app-server` headless and count server→client approval requests.

PTC_LIVE=1 plus a logged-in `codex` CLI required (subscription auth; never set
OPENAI_API_KEY — the CLI's own login carries auth). Skips cleanly when `codex` is
absent so the keyless tier never depends on it.

The question the spike answers is narrow: which `thread/start` params make a trivial
turn complete with ZERO server→client requests? Everything else here exists to make
that answer trustworthy —

  * every wire shape is taken from the schema the *installed* binary generates
    (`codex app-server generate-json-schema --out DIR`), not from prose, because the
    in-repo protocol crate is a fork that may lead or lag the installed CLI;
  * the auto-accept responder is shape-aware per method. A blanket
    `{"decision": "accept"}` is invalid for most of the ten server→client requests
    (permissions want `permissions`, elicitations want `action`, the legacy
    v1 approvals want `"approved"` not `"accept"`), and an invalid reply looks
    like "no approvals arrived" from the outside — the exact failure this spike
    must not make. Every reply is recorded whether or not it was needed;
  * the whole session is transcribed to an artifacts dir, so the verdict rests on
    raw JSON-RPC lines rather than on this script's summary of them.

Usage: PTC_LIVE=1 uv run --group dev python test/spikes/s4_codex_headless.py
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

CLIENT_NAME = "ptc_spike_s4"
CLIENT_VERSION = "0.1.0"
PROMPT = "Reply with exactly this and nothing else: CODEX-OK"
TURN_TIMEOUT_S = 300


def artifacts_dir() -> Path:
    d = Path(os.environ.get("PTC_S4_ARTIFACTS") or "/tmp/ptc-s4-codex-headless")
    d.mkdir(parents=True, exist_ok=True)
    return d


def auto_reply(method: str, params: dict) -> dict:
    """The fallback path: a result payload valid for each server→client request.

    Keyed by the ten `ServerRequest` methods the installed binary declares. The
    payloads are the *permissive* branch of each response schema where one exists,
    and an explicit refusal where accepting would mean fabricating user data
    (elicitations, free-text questions) or a credential we do not hold.
    """
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "accept"}
    if method in ("execCommandApproval", "applyPatchApproval"):
        # Legacy v1 approvals use ReviewDecision, whose accept value is "approved".
        return {"decision": "approved"}
    if method == "item/permissions/requestApproval":
        # Granted subset; echoing the request grants exactly what was asked for.
        return {"permissions": params.get("permissions", {}), "scope": "turn"}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None}
    if method == "item/tool/requestUserInput":
        return {"answers": {}}
    if method == "item/tool/call":
        return {"contentItems": [{"type": "inputText", "text": ""}], "success": False}
    # attestation/generate and account/chatgptAuthTokens/refresh are host-credential
    # requests we cannot satisfy; a JSON-RPC error is the honest reply.
    return {}


class AppServer:
    """Minimal stdio JSON-RPC client for `codex app-server` (seeds T22's real client).

    Reads on a background thread into a queue: a server→client request can arrive at
    any moment, including while we are blocked waiting for a response, and a bare
    `readline()` gives no way to bound that wait.
    """

    def __init__(self, log: Path, stderr_path: Path):
        self._stderr = stderr_path.open("w")
        self.p = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, bufsize=1)
        self.next_id = 0
        self.log = log.open("w")
        self.events: list[dict] = []
        self.server_requests: list[dict] = []
        self.notifications: list[str] = []
        self._q: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _record(self, direction: str, obj: dict) -> None:
        self.log.write(json.dumps({"t": round(time.time(), 3), "dir": direction,
                                   "msg": obj}) + "\n")
        self.log.flush()

    def _pump(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                self._q.put({"_unparsable": line})
        self._q.put(None)

    def send(self, obj: dict) -> None:
        self._record("out", obj)
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def _next(self, deadline: float) -> dict:
        msg = self._q.get(timeout=max(0.1, deadline - time.time()))
        if msg is None:
            raise RuntimeError("app-server closed stdout")
        self._record("in", msg)
        self.events.append(msg)
        if "method" in msg and "id" in msg:
            # Server→client REQUEST: the thing this spike is counting.
            m = msg["method"]
            print("SERVER_REQUEST:", m)
            self.server_requests.append(msg)
            self.send({"id": msg["id"], "result": auto_reply(m, msg.get("params") or {})})
        elif "method" in msg:
            self.notifications.append(msg["method"])
        return msg

    def request(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._next(deadline)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']}")
                return msg["result"]
        raise TimeoutError(method)

    def drain_until(self, method: str, timeout: float = TURN_TIMEOUT_S) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._next(deadline)
            if msg.get("method") == method:
                return msg
        raise TimeoutError(method)

    def close(self) -> None:
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.log.close()
        self._stderr.close()


def main() -> int:
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP: set PTC_LIVE=1")
        return 0
    if shutil.which("codex") is None:
        print("SKIP: no `codex` on PATH")
        return 0

    art = artifacts_dir()
    ver = subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()
    print("codex:", ver)

    s = AppServer(art / "wire.jsonl", art / "app-server.stderr")
    note: dict = {"codex_version": ver, "cwd": os.getcwd(), "t_start": time.time()}
    try:
        # 1. initialize. clientInfo.version is REQUIRED by InitializeParams.
        init = s.request("initialize", {
            "clientInfo": {"name": CLIENT_NAME, "title": "PTC spike S4",
                           "version": CLIENT_VERSION}})
        note["initialize"] = init
        print("initialize:", json.dumps(init))

        # 2. the handshake is two messages: any request before `initialized` is refused.
        s.notify("initialized")

        # 3. the params under test.
        start_params = {"cwd": os.getcwd(), "approvalPolicy": "never",
                        "sandbox": "read-only"}
        note["thread_start_params"] = start_params
        th = s.request("thread/start", start_params)
        note["thread_start_response_keys"] = sorted(th.keys())
        note["thread_start_effective"] = {k: th.get(k) for k in
                                          ("approvalPolicy", "approvalsReviewer", "sandbox",
                                           "model", "modelProvider", "cwd")}
        tid = th["thread"]["id"]
        print("thread:", tid, "effective:", json.dumps(note["thread_start_effective"]))

        # 4. one turn. The response carries the turn id `turn/interrupt` needs.
        ts = s.request("turn/start", {"threadId": tid,
                                      "input": [{"type": "text", "text": PROMPT}]})
        turn_id = ts["turn"]["id"]
        note["turn_start_response"] = ts
        print("turn:", turn_id, "status:", ts["turn"].get("status"))

        done = s.drain_until("turn/completed")
        note["turn_completed"] = done["params"]
        status = done["params"]["turn"]["status"]
        texts = [e["params"]["item"]["text"] for e in s.events
                 if e.get("method") == "item/completed"
                 and (e.get("params") or {}).get("item", {}).get("type") == "agentMessage"]
        note["agent_texts"] = texts
        note["notification_methods"] = sorted(set(s.notifications))
        note["notification_order"] = s.notifications
        note["server_request_methods"] = [r["method"] for r in s.server_requests]
        print("turn status:", status)
        print("agent said:", texts)
        print("notifications seen:", note["notification_methods"])

        unattended = not s.server_requests and status == "completed" and any(
            "CODEX-OK" in t for t in texts)
        note["verdict"] = "promote" if unattended else "fallback"
        print("VERDICT:", "promote (zero approvals, turn completed unattended)"
              if unattended else
              f"fallback — approvals={note['server_request_methods']} status={status}")
    finally:
        note["t_end"] = time.time()
        (art / "run.json").write_text(json.dumps(note, indent=2, default=str))
        s.close()
        print("ARTIFACTS:", art)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Just enough of `codex app-server` for unit tests — and as STRICT as the real one.

A permissive fake is the failure mode this file exists to prevent. A client that answers
every server→client request with `{"decision": "accept"}` passes against a fake that never
inspects the reply, and then wedges against the real 0.146.0 app-server, which rejects that
payload for EIGHT of its ten request methods. So every shape here is checked against the
schema the installed binary generates (`codex app-server generate-json-schema --out DIR`),
and a bad shape FAILS THE TURN with a named reason rather than being ignored.

Strictness this fake enforces, each matching an observed behaviour of codex-cli 0.146.0:
  * `initialize` requires `clientInfo.name` AND `clientInfo.version`;
  * the `initialized` NOTIFICATION must arrive before any other request ("Not initialized");
  * `thread/start.sandbox` is the kebab-case `SandboxMode` string — the camelCase
    `SandboxPolicy` tag (`"workspaceWrite"`) is refused exactly as the real server refuses
    it (`unknown variant ... expected one of read-only, workspace-write,
    danger-full-access`), while unknown *keys* are ignored, also as the real server does;
  * `turn/interrupt` requires `threadId` AND `turnId`;
  * the two credential requests (`attestation/generate`,
    `account/chatgptAuthTokens/refresh`) must be answered with a JSON-RPC *error* — an
    empty `{}` result is not a valid response to either, and accepting one here would make
    the client's known gap invisible.

Markers in the turn's prompt text drive the wire:
  ``REQ:<method>``  emit that server→client request mid-turn and validate the reply
  ``SLOW``          never finish on its own; wait for `turn/interrupt`
  anything else     complete at once, echoing ``echo:<text>``
Pass ``--trace <path>`` to append every received message as JSONL, so a test can assert on
the exact bytes a client put on the wire. It is an ARGV option rather than an environment
variable because the backend builds its child's environment from an allowlist and forwards
nothing else — a fake configured through the environment would be untraceable there, which
is exactly the property the allowlist exists to have.
"""
import json
import sys

#: Where `--trace` writes, set in main().
TRACE_PATH = None

#: The ten server→client request methods `ServerRequest` declares on 0.146.0.
SERVER_REQUESTS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/permissions/requestApproval",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "attestation/generate",
    "applyPatchApproval",
    "execCommandApproval",
)
#: Host-credential requests: only a real token satisfies them, so an honest client replies
#: with a JSON-RPC error. `{}` is NOT a valid result for either.
CREDENTIAL_REQUESTS = ("attestation/generate", "account/chatgptAuthTokens/refresh")

_CMD_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}
_FILE_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}
#: ReviewDecision (legacy v1 approvals). Note there is no "accept" and no plain "denied".
_REVIEW_DECISIONS = {"approved", "approved_for_session", "timed_out", "abort"}
_REVIEW_OBJECT_KEYS = {"approved_execpolicy_amendment", "network_policy_amendment", "denied"}
_ELICIT_ACTIONS = {"accept", "decline", "cancel"}
_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def trace(obj):
    if TRACE_PATH:
        with open(TRACE_PATH, "a") as fh:
            fh.write(json.dumps(obj) + "\n")


def err(rid, code, message):
    send({"id": rid, "error": {"code": code, "message": message}})


def _enum_decision(value, plain, object_keys=()):
    if isinstance(value, str):
        return value in plain
    if isinstance(value, dict) and object_keys:
        return len(value) == 1 and next(iter(value)) in object_keys
    return False


def _bad_instructions(p):
    """`ThreadStartParams`/`ThreadResumeParams` both declare `developerInstructions` and
    `baseInstructions` as `["string","null"]`. Anything else is a type error."""
    for field in ("developerInstructions", "baseInstructions"):
        v = p.get(field)
        if v is not None and not isinstance(v, str):
            return f"{field} must be a string or null, got {type(v).__name__}"
    return None


def bad_reply(method, msg):
    """Why the client's answer to `method` would be refused, or None if it is valid."""
    if method in CREDENTIAL_REQUESTS:
        if "error" not in msg:
            return (f"{method} needs a real credential; a client that cannot mint one must "
                    f"reply with a JSON-RPC error, got result={msg.get('result')!r}")
        return None
    if "error" in msg:
        return f"{method} was answered with an error instead of a result: {msg['error']}"
    r = msg.get("result")
    if not isinstance(r, dict):
        return f"{method}: result must be an object, got {r!r}"
    if method == "item/commandExecution/requestApproval":
        if not _enum_decision(r.get("decision"), _CMD_DECISIONS,
                              {"acceptWithExecpolicyAmendment", "applyNetworkPolicyAmendment"}):
            return f"{method}: bad CommandExecutionApprovalDecision {r.get('decision')!r}"
    elif method == "item/fileChange/requestApproval":
        if not _enum_decision(r.get("decision"), _FILE_DECISIONS):
            return f"{method}: bad FileChangeApprovalDecision {r.get('decision')!r}"
    elif method in ("execCommandApproval", "applyPatchApproval"):
        if not _enum_decision(r.get("decision"), _REVIEW_DECISIONS, _REVIEW_OBJECT_KEYS):
            return (f"{method}: bad ReviewDecision {r.get('decision')!r} "
                    "(the v1 approvals accept 'approved', never 'accept')")
    elif method == "item/permissions/requestApproval":
        if not isinstance(r.get("permissions"), dict):
            return f"{method}: 'permissions' (GrantedPermissionProfile) is required"
        if "scope" in r and r["scope"] not in ("turn", "session"):
            return f"{method}: bad PermissionGrantScope {r['scope']!r}"
    elif method == "mcpServer/elicitation/request":
        if r.get("action") not in _ELICIT_ACTIONS:
            return f"{method}: bad McpServerElicitationAction {r.get('action')!r}"
    elif method == "item/tool/requestUserInput":
        answers = r.get("answers")
        if not isinstance(answers, dict):
            return f"{method}: 'answers' must be an object of question id -> answer"
        for qid, a in answers.items():
            if not isinstance(a, dict) or not isinstance(a.get("answers"), list):
                return f"{method}: answer for {qid!r} must be {{'answers': [str]}}"
    elif method == "item/tool/call":
        if not isinstance(r.get("contentItems"), list) or not isinstance(r.get("success"), bool):
            return f"{method}: 'contentItems' (array) and 'success' (bool) are required"
    return None


class Fake:
    def __init__(self):
        self.initialized = False
        self.threads = 0
        self.next_server_id = 900
        self.outstanding = {}        # server-request id -> method
        self.turn = None             # in-flight {"id","threadId","text"}

    # -- turn lifecycle ---------------------------------------------------
    def finish_turn(self, status="completed", reason=None):
        t, self.turn = self.turn, None
        if t is None:
            return
        text = t["text"]
        if "HUGE" in text:
            # One line far past asyncio's default 64 KiB StreamReader limit. The real
            # app-server does this routinely (an mcpServerStatus/list result carrying MCP
            # tool catalogs measured >100 KB live), and NDJSON cannot continue a line.
            text = "H" * 200_000
        if status == "completed":
            send({"method": "item/started",
                  "params": {"threadId": t["threadId"], "turnId": t["id"],
                             "item": {"type": "userMessage", "id": "u1",
                                      "content": [{"type": "text", "text": t["text"]}]}}})
            send({"method": "item/completed",
                  "params": {"threadId": t["threadId"], "turnId": t["id"], "completedAtMs": 0,
                             "item": {"type": "userMessage", "id": "u1",
                                      "content": [{"type": "text", "text": t["text"]}]}}})
            send({"method": "item/agentMessage/delta",
                  "params": {"threadId": t["threadId"], "turnId": t["id"], "itemId": "m1",
                             "delta": "echo:"}})
            send({"method": "item/completed",
                  "params": {"threadId": t["threadId"], "turnId": t["id"], "completedAtMs": 0,
                             "item": {"type": "agentMessage", "id": "m1",
                                      "phase": "final_answer", "text": f"echo:{text}"}}})
        send({"method": "thread/tokenUsage/updated",
              "params": {"threadId": t["threadId"], "turnId": t["id"],
                         "tokenUsage": {"last": _usage(), "total": _usage(),
                                        "modelContextWindow": 400000}}})
        turn = {"id": t["id"], "items": [], "itemsView": "summary", "status": status,
                "durationMs": 7}
        if reason:
            turn["error"] = {"message": reason}
        send({"method": "turn/completed",
              "params": {"threadId": t["threadId"], "turn": turn}})

    def ask(self, method):
        self.next_server_id += 1
        rid = self.next_server_id
        self.outstanding[rid] = method
        send({"id": rid, "method": method, "params": _request_params(method, self.turn)})

    # -- dispatch ---------------------------------------------------------
    def on_reply(self, msg):
        method = self.outstanding.pop(msg.get("id"), None)
        if method is None:
            return
        why = bad_reply(method, msg)
        if why:
            self.finish_turn("failed", why)
        else:
            self.finish_turn("completed")

    def on_request(self, m, rid, p):
        if m == "initialize":
            ci = p.get("clientInfo") or {}
            if not ci.get("name") or not ci.get("version"):
                return err(rid, -32602, "clientInfo.name and clientInfo.version are required")
            return send({"id": rid, "result": {"userAgent": "fake-codex/0.146.0",
                                               "codexHome": "/dev/null",
                                               "argv": sys.argv[1:]}})
        if not self.initialized:
            return err(rid, -32600, "Not initialized")
        if m == "thread/start":
            sandbox = p.get("sandbox")
            if sandbox is not None and sandbox not in _SANDBOX_MODES:
                return err(rid, -32600, f"Invalid request: unknown variant `{sandbox}`, "
                                        "expected one of `read-only`, `workspace-write`, "
                                        "`danger-full-access`")
            if (why := _bad_instructions(p)):
                return err(rid, -32602, why)
            self.threads += 1
            tid = f"th-{self.threads}"
            return send({"id": rid, "result": {
                "thread": {"id": tid}, "cwd": p.get("cwd"),
                "approvalPolicy": p.get("approvalPolicy"),
                "approvalsReviewer": "auto_review",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "model": p.get("model") or "fake-model", "modelProvider": "openai"}})
        if m == "thread/resume":
            if not p.get("threadId"):
                return err(rid, -32602, "threadId is required")
            if (why := _bad_instructions(p)):
                return err(rid, -32602, why)
            return send({"id": rid, "result": {
                "thread": {"id": p["threadId"]}, "cwd": p.get("cwd"),
                "approvalPolicy": p.get("approvalPolicy"),
                "approvalsReviewer": "auto_review",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "model": "fake-model", "modelProvider": "openai"}})
        if m == "turn/start":
            if not p.get("threadId"):
                return err(rid, -32602, "threadId is required")
            items = p.get("input")
            if not isinstance(items, list) or not items or items[0].get("type") != "text":
                return err(rid, -32602, "input must be a non-empty array of UserInput")
            text = items[0]["text"]
            self.turn = {"id": f"tu-{self.threads}", "threadId": p["threadId"], "text": text}
            send({"id": rid, "result": {"turn": {"id": self.turn["id"], "items": [],
                                                 "itemsView": "notLoaded",
                                                 "status": "inProgress"}}})
            send({"method": "turn/started",
                  "params": {"threadId": p["threadId"], "turn": {"id": self.turn["id"],
                                                                "items": [],
                                                                "status": "inProgress"}}})
            marker = next((r for r in SERVER_REQUESTS if f"REQ:{r}" in text), None)
            if marker:
                return self.ask(marker)
            if "SLOW" in text:
                return None
            return self.finish_turn("completed")
        if m == "turn/interrupt":
            if not p.get("threadId") or not p.get("turnId"):
                return err(rid, -32602, "threadId and turnId are required")
            if self.turn and p["turnId"] != self.turn["id"]:
                return err(rid, -32602, f"no such turn {p['turnId']}")
            send({"id": rid, "result": {}})
            return self.finish_turn("interrupted")
        return err(rid, -32601, f"Method not found: {m}")


def _usage():
    return {"cachedInputTokens": 11, "inputTokens": 42, "outputTokens": 8,
            "reasoningOutputTokens": 0, "totalTokens": 50}


def _request_params(method, turn):
    """Params shaped like the real request, so a client keying off them sees real fields."""
    base = {"threadId": (turn or {}).get("threadId"), "turnId": (turn or {}).get("id"),
            "itemId": "i1", "startedAtMs": 0}
    if method == "item/permissions/requestApproval":
        return {**base, "cwd": ".", "reason": "needs write",
                "permissions": {"fileSystem": {"write": ["/tmp/x"]}}}
    if method == "mcpServer/elicitation/request":
        return {**base, "message": "who are you?", "requestedSchema": {"type": "object"}}
    if method == "item/tool/requestUserInput":
        return {**base, "questions": [{"id": "q1", "prompt": "pick one"}]}
    if method == "item/tool/call":
        return {**base, "tool": "t", "namespace": "n", "arguments": {}}
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"conversationId": "c1", "callId": "c", "command": ["rm", "-rf", "/tmp/x"]}
    return {**base, "command": ["rm", "-rf", "/tmp/x"]}


def main():
    global TRACE_PATH
    if "--trace" in sys.argv:
        TRACE_PATH = sys.argv[sys.argv.index("--trace") + 1]
    fake = Fake()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        trace(msg)
        if "method" not in msg:
            fake.on_reply(msg)
            continue
        m, rid, p = msg["method"], msg.get("id"), msg.get("params") or {}
        if m == "initialized" and rid is None:
            fake.initialized = True
            continue
        if rid is None:
            continue                      # some other client notification: nothing to do
        fake.on_request(m, rid, p)


if __name__ == "__main__":
    main()

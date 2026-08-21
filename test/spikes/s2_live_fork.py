"""S2: fork a LIVE claude session from inside a tool call it is currently running.

PTC_LIVE=1 required, plus a logged-in `claude` (subscription auth; never set
ANTHROPIC_API_KEY). Two phases:

  phase A (this script, no args): start a parent via `claude -p` whose single turn
    (1) carries a MARKER in the user prompt, (2) has the assistant invent and state a
    second phrase of its own, and (3) mid-turn runs a Bash tool call invoking THIS
    script's --fork phase with $CLAUDE_CODE_SESSION_ID.
  phase B (--fork <sid>): snapshot the parent's transcript JSONL *as it stands at fork
    time*, then use claude-agent-sdk resume+fork_session to ask the child for both
    phrases.

Two markers, not one, because they discriminate. MARKER lives in the parent's user
message (flushed at turn start, so recalling it proves only that the fork saw the turn's
opening). INVENTED is authored by the parent assistant inside the same in-flight turn and
is random, so it cannot be re-derived from the prompt — recalling *it* is what proves the
fork saw a mid-turn assistant message. The transcript snapshot is the mechanical half:
it records exactly which JSONL records existed at fork time, so "partially flushed" is
characterized by observation rather than inferred from what the model said.
"""
import asyncio
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKER = "quokka-basilisk-42"
FORK_TIMEOUT_S = 240


def _secretish(name: str, value: str) -> bool:
    return (any(w in name.upper() for w in ("KEY", "TOKEN", "BEARER", "SECRET"))
            or value.startswith("sk-ant-"))


def artifacts_dir() -> Path:
    d = Path(os.environ.get("PTC_S2_ARTIFACTS") or "/tmp/ptc-s2-live-fork")
    d.mkdir(parents=True, exist_ok=True)
    return d


def transcript_for(sid: str) -> Path | None:
    """~/.claude/projects/<slug>/<sid>.jsonl — found by search, not by slug arithmetic."""
    hits = glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl"))
    return Path(hits[0]) if hits else None


def summarize(path: Path) -> dict:
    """Record-by-record shape of a transcript, plus where each marker string appears."""
    raw = path.read_bytes()
    rows, bad = [], 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            bad += 1
    out = []
    for i, r in enumerate(rows):
        msg = r.get("message") or {}
        content = msg.get("content")
        kinds = []
        if isinstance(content, str):
            kinds = [f"str[{len(content)}]"]
        elif isinstance(content, list):
            for b in content:
                k = b.get("type")
                if k == "tool_use":
                    kinds.append(f"tool_use:{b.get('name')}")
                elif k == "text":
                    kinds.append(f"text[{len(b.get('text', ''))}]")
                else:
                    kinds.append(str(k))
        blob = json.dumps(r)
        out.append({
            "i": i, "type": r.get("type"), "role": msg.get("role"),
            "ts": r.get("timestamp"), "blocks": kinds,
            "has_marker": MARKER in blob,
            "uuid": str(r.get("uuid"))[:8],
        })
    return {"path": str(path), "bytes": len(raw), "records": len(rows),
            "unparsable_tail_lines": bad, "rows": out}


def invented_from_snapshot(path: Path) -> str:
    """The assistant's own in-turn phrase, read out of the at-fork snapshot.

    The snapshot — not the parent's stdout — is the authority on what had been flushed
    when the child forked, and the parent's closing summary need not repeat the line.
    """
    for line in reversed(path.read_text().splitlines()):
        if not line.strip():
            continue
        rec = json.loads(line)
        msg = rec.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for b in msg.get("content") or []:
            if b.get("type") == "text" and "INVENTED:" in b.get("text", ""):
                return b["text"].split("INVENTED:", 1)[1].strip()
    return ""


async def fork_and_ask(sid: str) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, query

    art = artifacts_dir()
    note = {"phase": "fork", "sid_from_env_arg": sid, "cwd": os.getcwd(),
            # Names only for anything credential-shaped: a Claude Code tool env carries
            # CLAUDE_ROUTINE_BEARER (an sk-ant-oat OAuth token) among the CLAUDE_* vars.
            "claude_env": {k: ("<redacted>" if _secretish(k, v) else v)
                           for k, v in os.environ.items() if k.startswith("CLAUDE")},
            "anthropic_api_key_present": "ANTHROPIC_API_KEY" in os.environ,
            "t_start": time.time()}

    tpath = transcript_for(sid)
    if tpath is None:
        note["error"] = "no transcript found for sid"
        (art / "fork_phase.json").write_text(json.dumps(note, indent=2))
        print("FORK_ERROR: no transcript for", sid)
        return

    # Snapshot BEFORE the fork: this is the partially-flushed state the child will read.
    shutil.copy2(tpath, art / "parent_at_fork.jsonl")
    note["snapshot_at_fork"] = summarize(art / "parent_at_fork.jsonl")
    (art / "fork_phase.json").write_text(json.dumps(note, indent=2))
    print("FORK_SNAPSHOT records=%d bytes=%d" % (
        note["snapshot_at_fork"]["records"], note["snapshot_at_fork"]["bytes"]))

    opts = ClaudeAgentOptions(
        resume=sid, fork_session=True, max_turns=1, tools=[],
        setting_sources=[],  # S1: default None inherits the user's whole config + hooks
        permission_mode="bypassPermissions")
    prompt = ("Two facts may have been established earlier in THIS conversation. Reply "
              "with exactly two lines and nothing else:\n"
              "MARKER: <the marker phrase, or NONE if it is not in this conversation>\n"
              "INVENTED: <the invented hyphenated phrase, or NONE if it is not in this "
              "conversation>")

    texts, seq, result = [], [], None
    try:
        async def run():
            nonlocal result
            async for m in query(prompt=prompt, options=opts):
                seq.append(type(m).__name__)
                result = m
                if type(m).__name__ == "AssistantMessage":
                    for b in m.content:
                        t = getattr(b, "text", "")
                        if t:
                            texts.append(t)
                            print("FORK_SAW:", t.replace("\n", " | "))
        await asyncio.wait_for(run(), timeout=FORK_TIMEOUT_S)
    except BaseException as e:  # noqa: BLE001 - the spike wants the failure recorded
        note["fork_exception"] = f"{type(e).__name__}: {e}"
        print("FORK_EXCEPTION:", type(e).__name__, e)

    note["msg_seq"] = seq
    note["fork_texts"] = texts
    note["child_session_id"] = getattr(result, "session_id", None)
    note["result_fields"] = {k: getattr(result, k, None) for k in
                             ("subtype", "is_error", "num_turns", "terminal_reason",
                              "total_cost_usd")}
    note["t_end"] = time.time()

    # Did the fork touch the parent's own transcript file? (It must not.)
    shutil.copy2(tpath, art / "parent_after_fork.jsonl")
    note["snapshot_after_fork"] = summarize(art / "parent_after_fork.jsonl")
    (art / "fork_phase.json").write_text(json.dumps(note, indent=2))
    print("FORK_CHILD_SESSION:", note["child_session_id"])
    print("FORK_RESULT:", json.dumps(note["result_fields"]))


def main() -> None:
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP: set PTC_LIVE=1")
        return
    art = artifacts_dir()
    cmd = f'{sys.executable} {os.path.abspath(__file__)} --fork "$CLAUDE_CODE_SESSION_ID"'
    prompt = (
        f"Remember this MARKER: {MARKER}.\n"
        "In this same turn, do exactly two things, in order:\n"
        "1. Write one line: INVENTED: <three unusual words you invent right now, "
        "hyphen-separated, nothing to do with the marker>\n"
        "2. Then run this exact bash command with the Bash tool (set its timeout to "
        f"600000 ms) and show its full output:\n{cmd}\n"
        "Do nothing else.")
    (art / "parent_prompt.txt").write_text(prompt)

    # Scrub the session identity of whatever session is running THIS script, so the
    # parent cannot possibly resolve to it: the whole probe hinges on the sid phase B
    # receives naming the parent and nothing else.
    env = dict(os.environ)
    launcher_sid = env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    t0 = time.time()
    r = subprocess.run(["claude", "-p", "--permission-mode", "bypassPermissions", prompt],
                       capture_output=True, text=True, timeout=900, env=env)
    (art / "parent_stdout.txt").write_text(r.stdout)
    (art / "parent_stderr.txt").write_text(r.stderr)
    print(r.stdout)
    if r.stderr.strip():
        print("PARENT_STDERR:", r.stderr[:2000])
    print(f"PARENT_ELAPSED {time.time() - t0:.1f}s rc={r.returncode}")

    note_path = art / "fork_phase.json"
    if not note_path.exists():
        print("VERDICT: inconclusive — fork phase produced no artifact")
        return
    note = json.loads(note_path.read_text())

    # Post-run comparison: what the parent's transcript looked like at fork time vs after.
    sid = note.get("sid_from_env_arg")
    note["launcher_sid"] = launcher_sid
    note["sid_is_launcher"] = (sid == launcher_sid)
    if note["sid_is_launcher"]:
        print("FORK_SID_LEAK: the parent resolved to the LAUNCHING session — probe invalid")
    final = transcript_for(sid) if sid else None
    if final:
        shutil.copy2(final, art / "parent_final.jsonl")
        note["snapshot_final"] = summarize(art / "parent_final.jsonl")
    child = note.get("child_session_id")
    cpath = transcript_for(child) if child else None
    if cpath:
        shutil.copy2(cpath, art / "child_forked.jsonl")
        note["snapshot_child"] = summarize(art / "child_forked.jsonl")
    note_path.write_text(json.dumps(note, indent=2))

    at_fork = note.get("snapshot_at_fork", {})
    print("FLUSH at_fork records=%s  after_fork records=%s  final records=%s  child records=%s" % (
        at_fork.get("records"),
        note.get("snapshot_after_fork", {}).get("records"),
        note.get("snapshot_final", {}).get("records"),
        note.get("snapshot_child", {}).get("records")))
    for row in at_fork.get("rows", []):
        print("  at_fork", row["i"], row["type"], row["role"], row["blocks"],
              "marker" if row["has_marker"] else "")

    forked = " ".join(note.get("fork_texts") or [])
    saw_marker = MARKER in forked
    invented = invented_from_snapshot(art / "parent_at_fork.jsonl")
    saw_invented = bool(invented) and invented.lower() in forked.lower()
    print(f"PARENT_INVENTED={invented!r} FORK_RECALLED_MARKER={saw_marker} "
          f"FORK_RECALLED_INVENTED={saw_invented}")
    # Promote needs both: the marker alone only shows the turn's opening user message
    # survived; the invented phrase is what proves a mid-turn assistant message did.
    print("VERDICT: promote" if (saw_marker and saw_invented) else "VERDICT: check fallback")
    print(f"ARTIFACTS: {art}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--fork":
        asyncio.run(fork_and_ask(sys.argv[2]))
        sys.exit(0)
    main()

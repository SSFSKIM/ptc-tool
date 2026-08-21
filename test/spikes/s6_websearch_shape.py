"""S6: where do WebSearch's structured results appear in the SDK message stream?

Spec criteria (verbatim): *"Promote → clean field mapping into `SearchResult`. Fallback:
best-effort extraction with `SearchResult.raw` retaining the source block — the return
type is `list[SearchResult]` either way."*

The spike runs ONE scoped query allowed only the WebSearch tool and dumps every message
and every content block it sees. Two things make the dump trustworthy rather than
indicative:

  * the options here are the ones `runtime/web.py` will use — `setting_sources=[]`
    included. A spike run with the host's settings loaded would exercise a different
    tool set and different hooks than production, so its "shape" would be evidence about
    a configuration nobody ships;
  * reprs are written UNTRUNCATED to an artifacts file next to this script. The console
    copy is clipped for readability; the verdict is read off the file, so one live run
    is enough and re-running to see a field that scrolled past is never needed.

Usage: PTC_LIVE=1 uv run --group dev python test/spikes/s6_websearch_shape.py
"""
import asyncio
import json
import os
from pathlib import Path

#: Outside the repo, like S4's: a raw stream dump is evidence for one run, not source.
ARTIFACTS = Path(os.environ.get("PTC_S6_ARTIFACTS") or "/tmp/ptc-s6-websearch-shape")
QUERY = "anthropic claude agent sdk release notes"


def _jsonable(o):
    """Best-effort structural dump: dataclasses/objects → dicts, everything else → repr."""
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    d = getattr(o, "__dict__", None)
    if d:
        return {"__type__": type(o).__name__, **{k: _jsonable(v) for k, v in d.items()}}
    return repr(o)


async def main():
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP: PTC_LIVE=1 required")
        return
    from claude_agent_sdk import ClaudeAgentOptions, query

    opts = ClaudeAgentOptions(
        tools=["WebSearch"], allowed_tools=["WebSearch"],
        permission_mode="bypassPermissions", max_turns=2,
        setting_sources=[],
    )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    dump = []
    async for m in query(prompt=f"Search the web for: {QUERY}", options=opts):
        print("==", type(m).__name__)
        blocks = getattr(m, "content", None)
        rec = {"message": type(m).__name__, "blocks": []}
        if isinstance(blocks, str):
            rec["content_str"] = blocks
            print("   content(str):", repr(blocks)[:400])
        for b in (blocks or []) if not isinstance(blocks, str) else []:
            print("   block:", type(b).__name__, repr(b)[:800])
            rec["blocks"].append({"type": type(b).__name__,
                                  "repr": repr(b),
                                  "struct": _jsonable(b)})
        if type(m).__name__ == "ResultMessage":
            rec["result"] = _jsonable(getattr(m, "result", None))
        dump.append(rec)

    out = ARTIFACTS / "stream.json"
    out.write_text(json.dumps(dump, indent=2, default=repr))
    print(f"\n[wrote {out} — {len(dump)} messages]")


asyncio.run(main())

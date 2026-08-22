"""`web_fetch` (pure httpx) and `web_search` (a WebSearch-scoped one-shot SDK query).

The two halves are deliberately asymmetric, and the asymmetry is the design:

  * `web_fetch` is PTC's spirit in one primitive — the WHOLE page comes back in the
    returned object and the caller filters it in Python. Nothing is summarized unless
    `prompt=` is passed, and even then the full text stays alongside the summary. It
    spawns no subprocess and buys no tokens.
  * `web_search` cannot be pure: Claude Code's WebSearch is a tool the CLI owns, so
    reaching it means a scoped one-shot SDK query. The runtime keeps the tool's own
    result and discards the model's prose around it — the prose is what the caller
    would have had to re-parse anyway, and the tool's list is the actual answer.

Spike S6 pinned the result shape live: the WebSearch `tool_result` block's `content` is a
STRING, not a list of dicts, and the machine-readable payload is a single `Links: [...]`
JSON array embedded in it, carrying `title` and `url` per hit (and no snippet). So the
promoted path is a JSON parse of that line, not a field read off a structured block, and
prose regex is only the last resort. See `_results_from_text`.
"""
import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlsplit

from . import audit
from .agents import AgentFailed, AgentOpts, child_ptc_env, guarded, shared_semaphore
from .claude_backend import terminal_failure

#: Hard body cap, enforced WHILE streaming: a limit checked after the download has already
#: finished is a report, not a cap.
_MAX_BYTES = 10_000_000

#: Tool names whose results carry search hits. "WebSearch" is the CLI's client-side tool
#: (what S6 observed); "web_search" is the SDK's server-tool spelling (`ServerToolUseBlock`),
#: accepted so a CLI that routes the search server-side keeps working.
_WEB_TOOL_NAMES = ("WebSearch", "web_search")

#: The S6-pinned payload: one line, `Links:` then a JSON array. Line-anchored on purpose —
#: a greedy/DOTALL variant would happily swallow the model's prose after it.
_LINKS_RE = re.compile(r"^Links:\s*(\[.*\])\s*$", re.M)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

#: Last-resort prose extraction: an optionally quoted/bracketed title in front of a URL.
_PROSE_LINK_RE = re.compile(r"(?:\"([^\"]+)\"|\[([^\]]+)\])?\s*\(?(https?://[^\s)\"'<>]+)")


@dataclass
class FetchResult:
    url: str            #: the FINAL url after redirects, not the one asked for
    status: int
    title: str
    text: str           #: the whole page — markdown when it was HTML, else as served
    summary: str | None = None   #: only when `prompt=` was passed


@dataclass
class SearchResult:
    title: str
    url: str
    #: Empty on the S6-pinned shape: the WebSearch tool returns title+url only. Kept in
    #: the contract because the prose fallback and any richer future payload can fill it —
    #: read it, never require it.
    snippet: str = ""
    #: The source record this result was read out of: the parsed hit dict on the promoted
    #: path, the raw block text on the fallback path. The spec's escape hatch for callers
    #: who need more than the three mapped fields.
    raw: object = None


# ---------------------------------------------------------------- web_fetch

def _is_html(raw: str, content_type: str) -> bool:
    """The Content-Type header decides; a server that sent none is judged on the body.
    Running markdownify over JSON or plain text would mangle it, so this is not cosmetic."""
    if content_type:
        return "html" in content_type.lower()
    head = raw[:2000].lower()
    return "<html" in head or "<!doctype html" in head


async def web_fetch(url: str, *, prompt: str | None = None,
                    timeout: float = 30.0) -> FetchResult:
    """GET `url`, follow redirects, return the whole page as markdown in `.text`.

    `prompt=` additionally runs `llm(prompt, over the text)` and fills `.summary` — the
    full text is still there; summarizing never replaces it. A summarization turn that
    FAILS propagates: a caller who asked for a summary did not get one, and `.summary=None`
    would claim the model had nothing to add. Fetch without `prompt=` for the page alone.

    Two deadlines, deliberately: `timeout` bounds the fetch (including the wait for a
    concurrency permit), while the optional summarization runs under `llm()`'s own
    default. A 30 s page deadline is generous; the same 30 s applied to a model call over
    a long page would mostly just fail.
    """
    audit.append("web_fetch", url=url[:200], summarize=bool(prompt))

    async def _get():
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                     headers={"User-Agent": "ptc/0.1"}) as c:
            async with c.stream("GET", url) as r:
                chunks, total = [], 0
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        raise ValueError(
                            f"response exceeds the {_MAX_BYTES} byte cap: {url}")
                    chunks.append(chunk)
                return (str(r.url), r.status_code, b"".join(chunks),
                        r.charset_encoding or "utf-8",
                        r.headers.get("content-type", ""))

    # The shared pool bounds the fetch itself — but the permit is RELEASED before the
    # summarization below. Holding it across `llm()`, which takes a permit of its own from
    # the same pool, would deadlock every caller at max_concurrency.
    final_url, status, body, encoding, ctype = await guarded(
        shared_semaphore(), _get, timeout)

    raw = body.decode(encoding, errors="replace")
    html = _is_html(raw, ctype)
    if html:
        from markdownify import markdownify
        text = markdownify(raw)
    else:
        text = raw
    m = _TITLE_RE.search(raw) if html else None
    title = unescape(m.group(1)).strip()[:300] if m else ""
    out = FetchResult(final_url, status, title, text)
    if prompt:
        from ._llm import llm
        out.summary = await llm(f"{prompt}\n\n<page url={final_url}>\n{text[:200_000]}\n</page>")
    return out


# --------------------------------------------------------------- web_search

def _results_from_text(text: str) -> list[SearchResult]:
    """One tool_result's text → hits. S6's `Links:` JSON first; prose regex only if it
    is absent, so the model's narrative URLs never dilute a good structured answer.

    A well-formed `Links:` payload always wins, even an EMPTY one (`Links: []`): a
    genuine zero-result search must return zero hits, not fall through and scrape the
    model's write-up for urls it merely mentions — that write-up is exactly the prose
    this function exists to discard, not a source of results.

    Honest status of the fallback branch: it has never fired against real data. Every
    WebSearch response observed live (S6 and A7 alike) carried a well-formed `Links:`
    line, so the prose scrape is pinned only by a synthetic fixture in
    `test/unit/test_web.py` — keep it as insurance against a shape change, do not treat
    it as a path with field evidence behind it.
    """
    m = _LINKS_RE.search(text)
    if m:
        try:
            items = json.loads(m.group(1))
        except json.JSONDecodeError:
            items = None
        if isinstance(items, list):
            return [SearchResult(str(d.get("title") or "").strip(), d["url"],
                                 str(d.get("snippet") or ""), raw=d)
                    for d in items if isinstance(d, dict) and d.get("url")]
    return [SearchResult((mm.group(1) or mm.group(2) or "").strip(), mm.group(3), raw=text)
            for mm in _PROSE_LINK_RE.finditer(text)]


def _parse_blocks(blocks: list) -> list[SearchResult]:
    """Map WebSearch tool_result blocks into `SearchResult`s, deduplicated by url.

    Accepts three carriers so the parse survives a shape change without going silent: the
    S6 string content, a list of content parts (`{"type": "text", "text": ...}`), and a
    list of already-structured hit dicts.
    """
    results: list[SearchResult] = []
    for b in blocks:
        content = getattr(b, "content", b)
        for it in (content if isinstance(content, list) else [content]):
            if isinstance(it, dict):
                if it.get("url") and it.get("title"):
                    results.append(SearchResult(it["title"], it["url"],
                                                str(it.get("snippet") or ""), raw=it))
                    continue
                it = it.get("text")
            if isinstance(it, str):
                results.extend(_results_from_text(it))
    seen, out = set(), []
    for r in results:
        if r.url not in seen:
            seen.add(r.url)
            out.append(r)
    return out


def _select(seen: list, tool_names: dict) -> list:
    """Of every tool_result block in the stream, the ones that came from a web search.

    Prefer results correlated to an actual WebSearch `tool_use` id; fall back to every
    tool result only when NO correlation was possible at all, so a change in how
    tool_use ids are reported degrades into over-collecting rather than into silently
    returning nothing. Over-collecting is visible (odd urls); returning nothing looks
    like "the web had no answer". But when correlation DID work and every known id names
    a different tool, that is a real answer — no WebSearch ran — and must not widen to
    scrape whatever else the stream happened to carry: another tool's output can never
    be presented as search results.
    """
    picked = [b for tid, b in seen if tool_names.get(tid) in _WEB_TOOL_NAMES]
    return picked or ([] if tool_names else [b for _, b in seen])


def _domain_ok(url: str, allowed: list | None, blocked: list | None) -> bool:
    host = (urlsplit(url).hostname or "").lower()

    def hit(d: str) -> bool:
        d = (urlsplit(d).hostname or d).lower().lstrip("*.").strip("/")
        return bool(d) and (host == d or host.endswith("." + d))

    if blocked and any(hit(d) for d in blocked):
        return False
    return not allowed or any(hit(d) for d in allowed)


async def web_search(query_text: str, *, allowed_domains: list | None = None,
                     blocked_domains: list | None = None, max_results: int = 10,
                     timeout: float = 300.0) -> list[SearchResult]:
    """Search the web and return the tool's own hits, not the model's write-up.

    Domain filters go out as prompt hints (the tool's `allowed_domains`/`blocked_domains`
    inputs are the model's to set, not ours) AND are re-applied to the returned urls here,
    so a caller that asked for a restriction gets one whether or not the model honored the
    hint.

    Always returns `list[SearchResult]`: on the S6-pinned shape the fields are mapped from
    the tool's JSON; if that payload is ever missing, the same list comes back from a
    best-effort prose scrape with `.raw` holding the source block.

    `max_results` is truncation-only: the model decides how many hits the search tool
    turns up (S6/A7 observed 8), and PTC just slices the list down to at most
    `max_results` of them. A caller asking for more than the tool returned silently
    gets fewer — there is no way to make the tool search harder or return more hits.
    """
    from claude_agent_sdk import ClaudeAgentOptions, query
    audit.append("web_search", query=query_text[:200])
    hints = []
    if allowed_domains:
        hints.append(f"Only include results from these domains: {', '.join(allowed_domains)}.")
    if blocked_domains:
        hints.append(f"Exclude these domains: {', '.join(blocked_domains)}.")
    prompt = f"Search the web for: {query_text}. {' '.join(hints)}".strip()
    opts = ClaudeAgentOptions(
        tools=["WebSearch"], allowed_tools=["WebSearch"],
        permission_mode="bypassPermissions", max_turns=2,
        # Same isolation every other PTC child gets: no host settings, no foreign hooks,
        # and PTC's own child variables (a fresh PTC_SESSION above all — see child_ptc_env).
        setting_sources=[],
        env=child_ptc_env(AgentOpts()),
    )

    tool_names: dict = {}
    seen: list = []
    failed: list = []

    async def run():
        async for m in query(prompt=prompt, options=opts):
            # The terminal message is the only one that says whether the turn WORKED, and
            # it carries no list content — so the block loop below skipped it and an
            # authentication failure, a rate limit or a dead CLI came back as a search
            # that legitimately found nothing.
            reason = terminal_failure(m)
            if reason:
                failed.append(reason)
            content = getattr(m, "content", None)
            if not isinstance(content, list):
                continue
            for b in content:
                tid = getattr(b, "tool_use_id", None)
                if tid is not None:
                    seen.append((tid, b))
                elif getattr(b, "id", None) is not None and getattr(b, "name", None):
                    tool_names[b.id] = b.name

    await guarded(shared_semaphore(), run, timeout)
    if failed:
        raise AgentFailed(f"web_search({query_text[:80]!r}) did not run: {failed[-1]}")

    hits = [r for r in _parse_blocks(_select(seen, tool_names))
            if _domain_ok(r.url, allowed_domains, blocked_domains)]
    return hits[:max_results]


__all__ = ["FetchResult", "SearchResult", "web_fetch", "web_search"]

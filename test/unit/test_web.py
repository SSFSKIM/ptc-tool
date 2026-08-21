"""web_fetch / web_search parsing, keyless: a local HTTP server for the fetch half and the
REAL block spike S6 captured for the search half.

The search fixture below is not invented. It is the `ToolResultBlock` a live WebSearch
turn produced on 2026-08-22 (spike `test/spikes/s6_websearch_shape.py`), abridged only by
dropping five of the eight `Links` entries — every character that remains, including the
model's prose tail and the trailing REMINDER, is verbatim. That matters for the central
assertion here: the prose repeats two of the same urls (one with a sentence-ending period
glued on), so a parser that scraped prose alongside the structured payload would return
MORE than three hits. Three is the promoted path winning.
"""
import asyncio
import http.server
import json
import threading

import pytest
from claude_agent_sdk import ToolResultBlock

from ptc.runtime import web
from ptc.runtime.state import STATE
from ptc.runtime.web import (SearchResult, _domain_ok, _parse_blocks, _select,
                             web_fetch)

# -- the S6 fixture --------------------------------------------------------
S6_CONTENT = (
    'Web search results for query: "anthropic claude agent sdk release notes"\n'
    "\n"
    'Links: [{"title":"\U0001f680 v2.5.0-alpha.130+ SDK Release notes · Issue #782 · '
    'ruvnet/claude-flow","url":"https://github.com/ruvnet/claude-flow/issues/782"},'
    '{"title":"Claude Platform release notes - Claude Platform Docs",'
    '"url":"https://platform.claude.com/docs/en/release-notes/overview"},'
    '{"title":"Releases · anthropics/claude-agent-sdk-python",'
    '"url":"https://github.com/anthropics/claude-agent-sdk-python/releases"}]\n'
    "\n"
    "Based on the search results, here's what I found about the Claude Agent SDK:\n"
    "\n"
    "For the official Anthropic Claude Agent SDK release notes, you can access the Python "
    "releases directly at the GitHub repository "
    "(https://github.com/anthropics/claude-agent-sdk-python/releases) or the platform "
    "documentation at https://platform.claude.com/docs/en/release-notes/overview.\n"
    "\n\n"
    "REMINDER: You MUST include the sources above in your response to the user using "
    "markdown hyperlinks."
)
S6_BLOCK = ToolResultBlock(tool_use_id="toolu_01FXvPF29sbj6R72HyphiBRv", content=S6_CONTENT)


def _serve(handler_body, content_type="text/html"):
    """Start a throwaway localhost server returning `handler_body`; yields the base url."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = handler_body() if callable(handler_body) else handler_body
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# -- web_fetch -------------------------------------------------------------

def test_web_fetch_markdown_and_title(tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    srv = _serve(b"<html><head><title>T1 &amp; more</title></head>"
                 b"<body><h1>Hello</h1><p>World</p></body></html>")
    try:
        r = asyncio.run(web_fetch(f"http://127.0.0.1:{srv.server_port}/"))
        assert r.status == 200
        assert r.title == "T1 & more"        # entities decoded, not left as &amp;
        assert "Hello" in r.text and "World" in r.text
        assert "<h1>" not in r.text          # markdownified, not raw html
        assert r.summary is None             # no prompt= → nothing was summarized
    finally:
        srv.shutdown()


def test_web_fetch_non_html_is_not_markdownified(tmp_path):
    """A JSON (or plain-text) body must come back byte-faithful. markdownify over JSON
    eats the braces and quotes, which would make web_fetch useless for APIs."""
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    payload = b'{"a": ["<b>x</b>", 1]}'
    srv = _serve(payload, content_type="application/json")
    try:
        r = asyncio.run(web_fetch(f"http://127.0.0.1:{srv.server_port}/"))
        assert r.text == payload.decode()
        assert r.title == ""
        assert json.loads(r.text) == {"a": ["<b>x</b>", 1]}
    finally:
        srv.shutdown()


def test_web_fetch_size_cap_trips_mid_stream(tmp_path, monkeypatch):
    """The cap is enforced WHILE the body streams in, not measured after it has all
    landed in memory — an after-the-fact check is a report, not a cap.

    Proven, not asserted: the server hands out 64 MB in 1 MB slices and records whether
    it ever reached the last one. A post-download check would have to drain all 64 MB, so
    `finished` staying False is the evidence that the read stopped early. (The socket's
    own buffer lets a little past the cap; the margin below is far wider than any.)
    """
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    monkeypatch.setattr(web, "_MAX_BYTES", 1_000_000)
    finished = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(64):
                    self.wfile.write(b"x" * 1_000_000)
                finished.append(True)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ValueError, match="exceeds the 1000000 byte cap"):
            asyncio.run(web_fetch(f"http://127.0.0.1:{srv.server_port}/"))
    finally:
        srv.shutdown()
    assert not finished, "the whole 64 MB was read — the cap did not stop the stream"


def test_web_fetch_audits_the_url(tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    STATE.current_cell = 7
    STATE.cell_mutations = []
    srv = _serve(b"<html><title>t</title><body>b</body></html>")
    try:
        base = f"http://127.0.0.1:{srv.server_port}/"
        asyncio.run(web_fetch(base + "q" * 500))
    except Exception:
        pass
    finally:
        srv.shutdown()
    e = STATE.cell_mutations[-1]
    assert e["kind"] == "web_fetch" and e["cell"] == 7
    assert len(e["url"]) == 200 and e["summarize"] is False
    on_disk = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert on_disk[-1] == e


def test_web_fetch_summary_does_not_deadlock_against_llm(tmp_path, monkeypatch):
    """web_fetch releases its concurrency permit BEFORE summarizing.

    `llm()` takes a permit from the same shared pool, so a web_fetch that held its own
    permit across the summarization would deadlock every caller at max_concurrency=1 —
    the pool bound applies to the fetch, never across the model call.
    """
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 1, "depth": 0, "max_depth": 1}

    async def fake_run_once(task, o, *, resume=None, fork=False, bare_llm=False):
        from ptc.runtime.agents import AgentResult
        return AgentResult("SUMMARY", "s", None, 0, 1, 5)

    from ptc.runtime import _llm as llm_mod
    monkeypatch.setattr(llm_mod, "_run_once", fake_run_once)
    srv = _serve(b"<html><title>t</title><body>page body</body></html>")
    try:
        r = asyncio.run(asyncio.wait_for(
            web_fetch(f"http://127.0.0.1:{srv.server_port}/", prompt="summarize"), 10))
    finally:
        srv.shutdown()
    assert r.summary == "SUMMARY" and "page body" in r.text   # full text kept alongside


# -- web_search parsing ----------------------------------------------------

def test_parse_blocks_reads_the_real_s6_links_payload():
    out = _parse_blocks([S6_BLOCK])
    assert [r.url for r in out] == [
        "https://github.com/ruvnet/claude-flow/issues/782",
        "https://platform.claude.com/docs/en/release-notes/overview",
        "https://github.com/anthropics/claude-agent-sdk-python/releases",
    ]
    assert out[2].title == "Releases · anthropics/claude-agent-sdk-python"
    # Exactly three: the prose tail repeats two of these urls (one with a trailing
    # period), so any extra entry means prose was scraped on top of the structured hits.
    assert len(out) == 3
    # S6 verdict: the tool supplies title+url only. `.snippet` is honestly empty rather
    # than backfilled from the model's prose; `.raw` keeps the source record.
    assert all(r.snippet == "" for r in out)
    assert out[0].raw == {"title": out[0].title, "url": out[0].url}


def test_parse_blocks_prose_fallback_when_no_links_payload():
    """No `Links:` payload — the contract still returns list[SearchResult], best-effort,
    with the source block retained on `.raw` (the spec's S6 fallback, verbatim)."""
    class T:
        content = [{"type": "text", "text": 'Found "Doc B" (https://b.example/page) and more'}]
    out = _parse_blocks([T()])
    assert [(r.title, r.url) for r in out] == [("Doc B", "https://b.example/page")]
    assert isinstance(out[0], SearchResult) and out[0].raw == T.content[0]["text"]


def test_parse_blocks_accepts_structured_hit_dicts():
    """A future shape that hands back hit dicts directly maps straight through."""
    class B:
        content = [{"title": "Doc A", "url": "https://a.example", "snippet": "s"}]
    out = _parse_blocks([B()])
    assert (out[0].title, out[0].url, out[0].snippet) == ("Doc A", "https://a.example", "s")


def test_parse_blocks_dedupes_by_url():
    out = _parse_blocks([S6_BLOCK, S6_BLOCK])
    assert len(out) == 3


def test_parse_blocks_empty_links_is_zero_results_not_a_prose_scrape():
    """A genuine `Links: []` (a niche or over-constrained query) must return NO results —
    not fall through to the prose fallback and hand back urls the model merely mentioned
    while explaining the empty search. That prose is exactly what the promoted path exists
    to discard; treating it as a source of hits would fabricate results for a search that
    found nothing."""
    class T:
        content = ('Web search results for query: "x"\n\nLinks: []\n\n'
                   "I could not find results, but see https://made-up.example/doc "
                   "for background.")
    assert _parse_blocks([T()]) == []


# -- selection and domain filtering ---------------------------------------

class _Use:
    def __init__(self, id, name):
        self.id, self.name = id, name


class _Res:
    def __init__(self, tool_use_id, content=""):
        self.tool_use_id, self.content = tool_use_id, content


def test_select_prefers_correlated_websearch_results():
    seen = [("t1", _Res("t1", "web")), ("t2", _Res("t2", "other"))]
    names = {"t1": "WebSearch", "t2": "Bash"}
    assert [b.content for b in _select(seen, names)] == ["web"]


def test_select_falls_back_to_everything_when_nothing_correlates():
    seen = [("t1", _Res("t1", "a")), ("t2", _Res("t2", "b"))]
    assert [b.content for b in _select(seen, {})] == ["a", "b"]


def test_select_returns_nothing_when_correlation_names_a_different_tool():
    """Correlation succeeding and naming a non-WebSearch tool is a real answer — no
    WebSearch ran — and must not widen to scrape that other tool's output for urls. The
    over-collect fallback is reserved for when correlation was impossible altogether
    (`tool_names` empty), not for when it worked and said "not this one"."""
    seen = [("t1", _Res("t1", "ran ls: see https://evil.example/x"))]
    names = {"t1": "Bash"}
    assert _select(seen, names) == []


@pytest.mark.parametrize("url,allowed,blocked,ok", [
    ("https://docs.claude.com/x", ["claude.com"], None, True),
    ("https://evil.com/x", ["claude.com"], None, False),
    ("https://claude.com/x", ["claude.com"], None, True),
    ("https://notclaude.com/x", ["claude.com"], None, False),   # suffix, not substring
    ("https://a.example/x", None, ["example"], False),
    ("https://a.example/x", None, ["other.example"], True),
    ("https://a.example/x", ["a.example"], ["a.example"], False),  # block beats allow
    ("https://a.example/x", ["https://a.example/docs"], None, True),  # url-shaped domain
])
def test_domain_ok(url, allowed, blocked, ok):
    assert _domain_ok(url, allowed, blocked) is ok

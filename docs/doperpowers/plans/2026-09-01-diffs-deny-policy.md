# Governed Side Effects: Diffs + Deny Policy (v0.3 initiative 3) Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use doperpowers:subagent-driven-execution to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kernel-side `write`/`edit` record bounded unified diffs (rendered in cell results), and an opt-in user-owned deny policy trips `bash`/`write`/`edit`/`web_fetch` with an audited PermissionError — empty by default, never a sandbox.

**Architecture:** A small pure policy module (`src/ptc/policy.py`, stdlib-only, importable on both the adapter and kernel sides) parses `~/.ptc/policy.json` with an mtime cache and answers match/state questions; the governed wrappers in `src/ptc/runtime/` consult it and audit denials; `web_fetch` switches to a manual redirect loop so every hop is checked BEFORE it is requested. Diffs ride the existing mutation records and audit.jsonl (2000-char cap each) and render as a `diff:` block inside the established trailing-budget mechanics. An upgrade-skew gate refuses (explicit error, not recycle) to attach a policy-present ungoverned kernel; bootstrap records `governed: true` in meta.json.

**Tech Stack:** Python 3.12 stdlib (difflib, fnmatch, re, json), httpx (already a dependency, for the redirect loop), pytest.

**Spec:** `docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md`, section `## Structural follow-on (v0.3 line)` → "Initiative 3 — governed side effects: diffs and a deny policy" (schema v1 JSON block, the Upgrade skew gate paragraph, acceptance 1-6), plus its 2026-09-01 Decision Log entry (governance = diffs + deny, no ask tier; malformed fails closed-and-loud). Conflicts during execution resolve against the spec.

## Global Constraints

- Policy file: `~/.ptc/policy.json` (i.e. `paths.ptc_home() / "policy.json"`), path overridable via env `PTC_POLICY`. Absent file = empty policy = today's behavior, tested as such. A file that exists but does not parse (or has a malformed shape/regex/version) fails CLOSED AND LOUD for governed calls only; `read`, `llm`, `agent`, plain Python untouched.
- Schema v1, verbatim from the spec: `{"version": 1, "deny": [{"tools": ["bash"], "pattern": "<python regex, re.search against the final command string>"}, {"tools": ["write", "edit"], "path": "<glob against the resolved absolute path>"}, {"tools": ["web_fetch"], "pattern": "<regex against the URL>"}]}`.
- A match raises `PermissionError` naming the rule index and pattern; the denial itself is audited: `kind="denied"`, with `tool` and the offending value clipped to 200 chars.
- Diff cap: **2000 chars per mutation entry**, in both the mutation record and the audit.jsonl line; difflib unified diff with 3 context lines; `write` to an existing file diffs against its prior content, a new file diffs against empty.
- `web_fetch` policy evaluation is PER-HOP: redirects followed manually, every hop's URL checked before it is requested, original and final URLs both audited: one ADDITIONAL `kind="web_fetch"` line carrying `final_url`, only when a policy is active. The existing unconditional per-call web_fetch audit line stays untouched — no policy means no NEW KINDS of lines, not an empty audit file.
- Upgrade-skew gate (spec verbatim semantics): attach REFUSES — an explicit error, not a recycle, not a notice — when a policy file that is malformed or carries ≥1 deny rule exists AND the kernel's meta lacks `governed`, naming both exits (`restart()` to govern / remove the policy to keep the namespace). No policy file attaches exactly as v0.4.0 does.
- The F2 admission machinery, discovery, and the venv/GC layer are untouched.
- No version bumps, no push (controller ships after the final review). Run tests with `uv run pytest …` from the repo root.

**Orientation (read once):** audit lives kernel-side: `src/ptc/runtime/audit.py::append(kind, **fields)` writes one JSON line to `kernel_dir/audit.jsonl` AND appends to `STATE.cell_mutations`, which lands in the cell record's `mutations` list; `src/ptc/shape.py` renders trailing pieces FIRST inside the budget (`trailing_budget(cap)`) — footer (`footer_line`), error line, result line — then gives the body the remainder (shape.py:200-223). `src/ptc/runtime/files.py` is 46 lines (read it whole); `src/ptc/runtime/shell.py::bash` builds a display string `label` and audits `audit.append("bash", command=label[:2000])` — grep that line to find the spawn seam. `src/ptc/runtime/web.py::web_fetch` currently uses `httpx.AsyncClient(follow_redirects=True, …)` (web.py:100-101) and its `FetchResult.url` documents "the FINAL url after redirects". Meta writes merge (`discovery.write_meta`); the attach path with its gates is `kernel.py::ensure_kernel` (the `stale = (_venv_gone(key) or _protocol_mismatch(key))` block from initiative 1). Bootstrap is `src/ptc/runtime/bootstrap.py::install` (the peek wiring from initiative 2 shows the belt idiom).

---

### Task 1: The policy core — parse, cache, match, state

**Files:**
- Create: `src/ptc/policy.py`
- Test: `test/unit/test_policy.py` (new)

**Interfaces:**
- Consumes: `paths.ptc_home()` only.
- Produces (later tasks rely on these exact names):
  - `policy.Rule` — frozen dataclass: `index: int`, `tools: tuple[str, ...]`, `pattern: str | None`, `path: str | None`
  - `policy.PolicyError(RuntimeError)` — malformed policy; governed wrappers let it propagate (loud)
  - `policy.PolicyGateRefusal(RuntimeError)` — the attach gate's error type (Task 5 raises it; the CLI catches it)
  - `policy.policy_path() -> Path`
  - `policy.load_rules() -> list[Rule] | None` — None when absent; `[]` when valid-but-empty; PolicyError on malformed (bad JSON, wrong version, non-list deny, unknown rule shape, uncompilable regex); mtime-cached
  - `policy.match(tool: str, value: str) -> Rule | None` — first matching deny rule; bash/web_fetch rules use `re.search(pattern, value)`, write/edit rules use `fnmatch.fnmatch(value, path)`
  - `policy.file_state() -> str` — `"absent" | "empty" | "active" | "malformed"` (gate fires on active/malformed)

- [ ] **Step 1: Write the failing tests**

Create `test/unit/test_policy.py`:

```python
"""The deny-policy core: user-owned, empty by default, loud when malformed."""
import json
import time

import pytest
from ptc import policy


def _write(tmp_path, monkeypatch, obj) -> None:
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    p = tmp_path / "policy.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))


def test_absent_file_is_the_empty_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    assert policy.load_rules() is None
    assert policy.match("bash", "rm -rf /") is None
    assert policy.file_state() == "absent"


def test_valid_rules_load_and_match(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": [
        {"tools": ["bash"], "pattern": "^rm "},
        {"tools": ["write", "edit"], "path": "/etc/*"},
        {"tools": ["web_fetch"], "pattern": "internal\\.example"},
    ]})
    rules = policy.load_rules()
    assert [r.index for r in rules] == [0, 1, 2]
    assert policy.match("bash", "rm -rf /tmp/x").index == 0
    assert policy.match("bash", "echo rm") is None          # re.search, anchored by ^
    assert policy.match("edit", "/etc/hosts").index == 1
    assert policy.match("write", "/home/u/etc/hosts") is None
    assert policy.match("web_fetch", "https://internal.example/x").index == 2
    assert policy.match("llm", "anything") is None           # ungoverned tool names never match
    assert policy.file_state() == "active"


def test_empty_deny_is_empty_not_active(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": []})
    assert policy.load_rules() == []
    assert policy.file_state() == "empty"


@pytest.mark.parametrize("payload", [
    "not json at all",
    {"version": 2, "deny": []},
    {"version": 1, "deny": "nope"},
    {"version": 1, "deny": [{"tools": ["bash"]}]},              # no pattern and no path
    {"version": 1, "deny": [{"tools": ["bash"], "pattern": "("}]},   # uncompilable regex
    {"version": 1},                                              # deny missing entirely
])
def test_malformed_shapes_raise_policy_error_and_state_malformed(monkeypatch, tmp_path, payload):
    _write(tmp_path, monkeypatch, payload)
    with pytest.raises(policy.PolicyError):
        policy.load_rules()
    assert policy.file_state() == "malformed"


def test_mtime_cache_reloads_on_change(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": [{"tools": ["bash"], "pattern": "^a"}]})
    assert policy.match("bash", "abc") is not None
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^z"}]}))
    now = time.time() + 5
    import os
    os.utime(p, (now, now))                                  # force a visible mtime step
    assert policy.match("bash", "abc") is None
    assert policy.match("bash", "zzz") is not None


def test_ptc_policy_env_overrides_the_path(monkeypatch, tmp_path):
    alt = tmp_path / "elsewhere.json"
    alt.write_text(json.dumps({"version": 1, "deny": [{"tools": ["bash"], "pattern": "x"}]}))
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PTC_POLICY", str(alt))
    assert policy.policy_path() == alt
    assert policy.match("bash", "x marks") is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_policy.py -v`
Expected: collection ERROR (`ModuleNotFoundError`/`ImportError: cannot import name 'policy'`).

- [ ] **Step 3: Implement `src/ptc/policy.py`**

```python
"""The user-owned deny policy: empty by default, loud when malformed, never a sandbox.

Consulted by the governed kernel wrappers (`bash`, `write`, `edit`, `web_fetch`) and by
the adapter's attach gate. Raw Python was never governed — the audit-instead-of-guard-
rails boundary stands; this is a tripwire for the wrappers. The plugin ships NO rules:
the file is the user's.
"""
import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import ptc_home


@dataclass(frozen=True)
class Rule:
    index: int
    tools: tuple[str, ...]
    pattern: str | None    # regex, re.search — bash (final command string), web_fetch (URL)
    path: str | None       # glob, fnmatch — write/edit (resolved absolute path)


class PolicyError(RuntimeError):
    """The policy file exists but cannot be believed. Governed calls fail on this LOUDLY:
    a silently ignored typo would leave the user thinking themselves protected."""


class PolicyGateRefusal(RuntimeError):
    """The attach gate's refusal (kernel predates enforcement while a policy stands).
    Its own type so the CLI can print it as a sentence rather than a traceback."""


def policy_path() -> Path:
    raw = os.environ.get("PTC_POLICY")
    return Path(raw).expanduser() if raw else ptc_home() / "policy.json"


#: (path, mtime) -> parsed rules. One entry: the file is one file.
_cache: tuple[tuple[str, float], "list[Rule]"] | None = None


def _parse(text: str) -> "list[Rule]":
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise PolicyError(f"policy.json is not JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise PolicyError("policy.json must be an object with \"version\": 1")
    deny = doc.get("deny")
    if not isinstance(deny, list):
        raise PolicyError("policy.json needs a \"deny\" list (empty is fine)")
    rules: list[Rule] = []
    for i, raw in enumerate(deny):
        if not isinstance(raw, dict) or not isinstance(raw.get("tools"), list) \
                or not all(isinstance(t, str) for t in raw["tools"]):
            raise PolicyError(f"deny[{i}] needs a \"tools\" list of strings")
        pattern, path = raw.get("pattern"), raw.get("path")
        if (pattern is None) == (path is None):
            raise PolicyError(f"deny[{i}] needs exactly one of \"pattern\" or \"path\"")
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as e:
                raise PolicyError(f"deny[{i}] pattern does not compile: {e}") from e
        rules.append(Rule(i, tuple(raw["tools"]), pattern, path))
    return rules


def load_rules() -> "list[Rule] | None":
    """None = no file (the empty policy). PolicyError = a file that cannot be believed.
    Cached by (path, mtime): one stat per call, one parse per change."""
    global _cache
    p = policy_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    key = (str(p), mtime)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    rules = _parse(p.read_text())
    _cache = (key, rules)
    return rules


def match(tool: str, value: str) -> "Rule | None":
    rules = load_rules()
    if not rules:
        return None
    for r in rules:
        if tool not in r.tools:
            continue
        if r.pattern is not None and re.search(r.pattern, value):
            return r
        if r.path is not None and fnmatch.fnmatch(value, r.path):
            return r
    return None


def file_state() -> str:
    """absent | empty | active | malformed — what the attach gate keys off (it fires on
    active and malformed: an unbelievable file must gate exactly as a believed one)."""
    try:
        rules = load_rules()
    except PolicyError:
        return "malformed"
    if rules is None:
        return "absent"
    return "active" if rules else "empty"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest test/unit/test_policy.py -v`
Expected: PASS (11 collected items — 5 singles + the 6-way parametrized malformed test).

- [ ] **Step 5: Commit**

```bash
git add src/ptc/policy.py test/unit/test_policy.py
git commit -m "f5(ptc): policy core — schema-v1 deny rules, mtime cache, loud-malformed (v0.3 i3)"
```

---

### Task 2: Diffs + governance for `files` and `bash`

**Files:**
- Modify: `src/ptc/runtime/files.py` (whole file is 46 lines — diff recording + policy check)
- Modify: `src/ptc/runtime/shell.py` (policy check at the spawn seam)
- Modify: `test/conftest.py` (suite-wide policy isolation — see Step 0)
- Test: `test/unit/test_files.py` (extend), `test/unit/test_governed_wrappers.py` (new)

**Interfaces:**
- Consumes: from Task 1: `policy.match(tool, value) -> Rule | None`, `policy.PolicyError`.
- Produces: mutation entries for `write`/`edit` gain a `diff: str` field (≤2000 chars); denials audit as `kind="denied"` with `tool`, `rule` (index), `value` (clipped 200); a `_denied(tool, value)` helper in `src/ptc/runtime/files.py` that Task 3's web wrapper imports (`from .files import _denied`).

- [ ] **Step 0: Isolate the suite from the developer's real policy file**

Once wrappers are governed, any test that calls `files.write`/`shell.bash`/`web_fetch`
without pinning `PTC_HOME` would stat (and maybe parse) the REAL `~/.ptc/policy.json` —
green today only because none exists, and this initiative exists so the owner will
create one. Add to `test/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _no_real_policy(tmp_path, monkeypatch):
    """Governed wrappers must never consult the developer's real ~/.ptc/policy.json:
    point PTC_POLICY at a path that does not exist (= the empty policy). Tests that
    exercise policy delenv or override this themselves."""
    monkeypatch.setenv("PTC_POLICY", str(tmp_path / "no-policy.json"))
```

and add `monkeypatch.delenv("PTC_POLICY", raising=False)` inside the existing `ptc_home`
fixture (conftest.py:85-93), so integration tests that write `ptc_home/policy.json` get
the PTC_HOME-relative path. Commit rides with this task's commit.

- [ ] **Step 1: Write the failing tests**

Append to `test/unit/test_files.py` (it exists — read it first and follow its fixture
idiom for STATE/kernel_dir setup; it exercises `files.write`/`files.edit` against tmp
dirs with `STATE.kernel_dir` pointed at one):

```python
def test_write_and_edit_record_bounded_diffs(files_env, tmp_path):
    """`files_env` here stands for this file's existing setup idiom — reuse it."""
    from ptc.runtime import files
    from ptc.runtime.state import STATE
    target = tmp_path / "doc.txt"
    files.write(str(target), "alpha\nbeta\n")
    new_file_entry = STATE.cell_mutations[-1]
    assert new_file_entry["kind"] == "write"
    assert "+alpha" in new_file_entry["diff"]          # a new file diffs against empty
    files.edit(str(target), "beta", "gamma")
    entry = STATE.cell_mutations[-1]
    assert entry["kind"] == "edit"
    assert "-beta" in entry["diff"] and "+gamma" in entry["diff"]
    assert len(entry["diff"]) <= 2000


def test_huge_diff_is_capped_with_a_note(files_env, tmp_path):
    from ptc.runtime import files
    from ptc.runtime.state import STATE
    target = tmp_path / "big.txt"
    files.write(str(target), "x\n" * 5000)
    d = STATE.cell_mutations[-1]["diff"]
    assert len(d) <= 2000
    assert "truncated" in d
```

Create `test/unit/test_governed_wrappers.py`:

```python
"""Deny rules trip the governed wrappers; denials are audited; malformed is loud."""
import json

import pytest


@pytest.fixture
def governed(tmp_path, monkeypatch):
    """A kernel-side STATE rooted in tmp, with a policy file the test writes."""
    from ptc.runtime.state import STATE
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    kd = tmp_path / "kernels" / "gov"
    (kd / "cells").mkdir(parents=True)
    monkeypatch.setattr(STATE, "kernel_dir", kd)
    monkeypatch.setattr(STATE, "current_cell", 1)
    monkeypatch.setattr(STATE, "cell_mutations", [])
    def write_policy(obj):
        (tmp_path / "policy.json").write_text(
            obj if isinstance(obj, str) else json.dumps(obj))
    return write_policy


def _audit_lines(tmp_path):
    p = tmp_path / "kernels" / "gov" / "audit.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_denied_write_raises_and_audits(governed, tmp_path):
    from ptc.runtime import files
    governed({"version": 1, "deny": [{"tools": ["write", "edit"], "path": str(tmp_path / "sec" / "*")}]})
    with pytest.raises(PermissionError, match="rule 0"):
        files.write(str(tmp_path / "sec" / "x.txt"), "data")
    assert not (tmp_path / "sec" / "x.txt").exists()
    denied = [e for e in _audit_lines(tmp_path) if e["kind"] == "denied"]
    assert denied and denied[0]["tool"] == "write" and denied[0]["rule"] == 0


def test_denied_bash_raises_before_spawn(governed, tmp_path):
    import asyncio
    from ptc.runtime import shell
    governed({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]})
    with pytest.raises(PermissionError, match="rule 0"):
        asyncio.run(shell.bash("rm -rf /tmp/never", timeout=5))
    denied = [e for e in _audit_lines(tmp_path) if e["kind"] == "denied"]
    assert denied and denied[0]["tool"] == "bash" and "rm -rf" in denied[0]["value"]


def test_unmatched_calls_run_untouched(governed, tmp_path):
    import asyncio
    from ptc.runtime import files, shell
    governed({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]})
    out = files.write(str(tmp_path / "ok.txt"), "fine")
    assert "Wrote" in out
    r = asyncio.run(shell.bash("echo ok"))        # acceptance 1's second half: unaffected
    assert getattr(r, "code", 0) == 0 or "ok" in str(r)


def test_malformed_policy_is_loud_for_governed_and_silent_for_read(governed, tmp_path):
    from ptc import policy
    from ptc.runtime import files
    governed("this is not json")
    (tmp_path / "readable.txt").write_text("still readable")
    with pytest.raises(policy.PolicyError):
        files.write(str(tmp_path / "x.txt"), "data")
    import asyncio
    from ptc.runtime import shell
    with pytest.raises(policy.PolicyError):        # acceptance 4: bash("echo hi") raises
        asyncio.run(shell.bash("echo hi"))
    assert files.read(str(tmp_path / "readable.txt")) == "still readable"
```

(`test_files.py` has an AUTOUSE fixture `_audit_to_tmp` that already does the setup —
the right adaptation is simply DROPPING the `files_env` parameter from the two diff
tests; do not rebuild them on another fixture. Note the bash test drives the coroutine with a loop directly;
if `shell.bash`'s signature or event-loop needs differ, follow how `test_bash_argv.py`
invokes it and keep the assertions.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_files.py test/unit/test_governed_wrappers.py -v`
Expected: FAIL/ERROR — no `diff` key, no policy consultation, no denied audit lines.

- [ ] **Step 3: Implement in `src/ptc/runtime/files.py`**

Replace the file body (keeping `read` untouched):

```python
"""File primitives with Claude-Code-exact edit semantics. Mutations audit, with diffs."""
import difflib
from pathlib import Path

from ptc import policy

from . import audit

_DIFF_CAP = 2000   # INCLUSIVE of the truncation note: spec acceptance 2 binds ≤2000


def _denied(tool: str, value: str) -> None:
    """Consult the deny policy for one governed call; raise (and audit) on a match.
    PolicyError from a malformed file propagates — loud is the contract."""
    rule = policy.match(tool, value)
    if rule is None:
        return
    audit.append("denied", tool=tool, rule=rule.index, value=value[:200])
    raise PermissionError(
        f"denied by policy rule {rule.index} "
        f"({'pattern ' + rule.pattern if rule.pattern else 'path ' + rule.path}) — "
        f"policy file: {policy.policy_path()}")


def _diff(path: str, old: str, new: str) -> str:
    d = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))
    if len(d) > _DIFF_CAP:
        note = "\n…[diff truncated — the full change is in the file]"
        d = d[:_DIFF_CAP - len(note)] + note        # cap INCLUSIVE of the note
    return d
```

then `write`/`edit` become:

```python
def write(path, content: str) -> str:
    p = Path(path).expanduser()
    resolved = str(p.resolve())     # non-strict on 3.12: fine for a not-yet-existing file;
    _denied("write", resolved)      # the CHECKED string equals the AUDITED string
    old = p.read_text(errors="replace") if p.is_file() else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    audit.append("write", path=resolved, added=n, diff=_diff(resolved, old, content))
    return f"Wrote {resolved} ({n} lines)"


def edit(path, old: str, new: str, replace_all: bool = False) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    _denied("edit", str(p.resolve()))
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        raise ValueError(f"string not found in {p}")
    if n > 1 and not replace_all:
        raise ValueError(f"found {n} occurrences in {p}, need exactly 1 — "
                         "widen the snippet to make it unique, or pass replace_all=True")
    count = n if replace_all else 1
    replaced = text.replace(old, new, count)
    p.write_text(replaced)
    removed = len(old.splitlines()) * count
    added = len(new.splitlines()) * count
    audit.append("edit", path=str(p.resolve()), added=added, removed=removed,
                 diff=_diff(str(p.resolve()), text, replaced))
    return f"Edited {p.resolve()} (+{added}/−{removed})"
```


- [ ] **Step 4: Governance at bash's spawn seam**

In `src/ptc/runtime/shell.py`: `label` is computed at shell.py:426, the unconditional
`audit.append("bash", command=label[:2000])` sits at shell.py:430, and the spawn follows
at shell.py:434. Insert exactly one line BETWEEN the label computation and that audit
line (i.e. between 426 and 430):

```python
    _denied("bash", label)
```

with `from .files import _denied` added to the module imports. Nothing else moves — a
denied call raises before the bash audit line and before any spawn, leaving ONLY the
`denied` audit entry.

- [ ] **Step 5: Run to verify pass, then the neighbors**

Run: `uv run pytest test/unit/test_files.py test/unit/test_governed_wrappers.py test/unit/test_bash_argv.py test/unit/test_shell_groups.py -v`
Expected: PASS. (`test_mutation_footer.py` and `test_shape.py` may notice the new `diff`
key riding mutation entries — the footer only reads `kind`/`command`/`path` fields, so
they should hold; if one pins an exact entry dict, update that pin.)

- [ ] **Step 6: Commit**

```bash
git add src/ptc/runtime/files.py src/ptc/runtime/shell.py test/unit/test_files.py test/unit/test_governed_wrappers.py
git commit -m "f5(ptc): bounded diffs on write/edit + deny policy trips files/bash, denials audited (v0.3 i3)"
```

---

### Task 3: Per-hop governance for `web_fetch`

**Files:**
- Modify: `src/ptc/runtime/web.py` (manual redirect loop + policy + audit)
- Test: `test/unit/test_web.py` (extend)

**Interfaces:**
- Consumes: from Task 1: `policy.match`/`PolicyError`; from Task 2: `files._denied(tool, value)`.
- Produces: `web_fetch` behavior contract — every hop's URL checked BEFORE request; one ADDITIONAL `kind="web_fetch"` audit line carrying `url` + `final_url` (each clipped [:200]) when and only when a policy is active — the EXISTING per-call audit line at web.py:97 (`url[:200]`, `summarize`) is UNCHANGED and fires policy-or-not; `test_web_fetch_audits_the_url` pins it and must keep passing. `FetchResult.url` still the final URL.

- [ ] **Step 1: Write the failing tests**

Append to `test/unit/test_web.py` (it exists and already fakes httpx — read its idiom
first; httpx offers `httpx.MockTransport` which the snippets below use; adapt to the
file's existing fake style if it differs):

```python
def _redirect_transport():
    import httpx
    def handler(request):
        if request.url.host == "hop.example":
            return httpx.Response(302, headers={"location": "https://denied.example/x"})
        return httpx.Response(200, text="made it")
    return httpx.MockTransport(handler)


def test_web_fetch_denies_at_the_hop_not_after_it(monkeypatch, tmp_path):
    import asyncio
    import json
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    (tmp_path / "policy.json").write_text(json.dumps(
        {"version": 1, "deny": [{"tools": ["web_fetch"], "pattern": "denied\\.example"}]}))
    from ptc.runtime import web
    from ptc.runtime.state import STATE
    kd = tmp_path / "kernels" / "webgov"
    (kd / "cells").mkdir(parents=True)
    monkeypatch.setattr(STATE, "kernel_dir", kd)
    monkeypatch.setattr(STATE, "current_cell", 1)
    monkeypatch.setattr(STATE, "cell_mutations", [])
    with pytest.raises(PermissionError, match="rule 0"):
        asyncio.run(web.web_fetch("https://hop.example/r", _transport=_redirect_transport()))
    lines = [json.loads(l) for l in (kd / "audit.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "denied" and "denied.example" in e["value"] for e in lines)


def test_web_fetch_follows_redirects_and_audits_both_urls_under_policy(monkeypatch, tmp_path):
    import asyncio
    import json
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    (tmp_path / "policy.json").write_text(json.dumps(
        {"version": 1, "deny": [{"tools": ["web_fetch"], "pattern": "never-matches-zz"}]}))
    from ptc.runtime import web
    from ptc.runtime.state import STATE
    kd = tmp_path / "kernels" / "webok"
    (kd / "cells").mkdir(parents=True)
    monkeypatch.setattr(STATE, "kernel_dir", kd)
    monkeypatch.setattr(STATE, "current_cell", 1)
    monkeypatch.setattr(STATE, "cell_mutations", [])
    r = asyncio.run(web.web_fetch("https://hop.example/r", _transport=_redirect_transport()))
    assert "made it" in r.text and "denied.example" in r.url
    entries = [json.loads(l) for l in (kd / "audit.jsonl").read_text().splitlines()]
    wf = [e for e in entries if e["kind"] == "web_fetch" and "final_url" in e]
    assert wf and wf[0]["url"].startswith("https://hop.example") \
        and "denied.example" in wf[0]["final_url"]


def test_web_fetch_without_policy_audits_nothing_new(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    from ptc.runtime import web
    from ptc.runtime.state import STATE
    kd = tmp_path / "kernels" / "webnone"
    (kd / "cells").mkdir(parents=True)
    monkeypatch.setattr(STATE, "kernel_dir", kd)
    monkeypatch.setattr(STATE, "current_cell", 1)
    monkeypatch.setattr(STATE, "cell_mutations", [])
    r = asyncio.run(web.web_fetch("https://hop.example/r", _transport=_redirect_transport()))
    assert "made it" in r.text
    # the EXISTING per-call audit line still fires; what must be absent without a policy
    # is any denied entry and any final_url-carrying (governed) entry
    entries = [json.loads(l) for l in (kd / "audit.jsonl").read_text().splitlines()]
    assert not [e for e in entries if e["kind"] == "denied" or "final_url" in e]
```

(If `web_fetch` has no seam for a transport, add the private keyword-only parameter
`_transport=None` threaded into the `httpx.AsyncClient(transport=_transport, …)`
construction — test-only, defaulting to None, which httpx treats as "use the real one".)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_web.py -k "hop or audits" -v`
Expected: FAIL (no `_transport` seam / redirects auto-followed / no policy consult).

- [ ] **Step 3: Implement the manual hop loop in `src/ptc/runtime/web.py`**

The real shape of `web_fetch` (read it first, web.py:83-130): the request happens inside
a nested closure `_get()` as `async with c.stream("GET", url) as r:`, which enforces
`_MAX_BYTES` WHILE STREAMING (`aiter_bytes` loop that raises mid-stream) and returns a
5-tuple `(final_url, status, body, encoding, ctype)`; the closure runs under
`guarded(shared_semaphore(), _get, timeout)`, and web.py:97 audits every call
unconditionally. There is no `.get(url)` — do NOT introduce one: a plain `.get` would
buffer the body and destroy the mid-stream cap that
`test_web_fetch_size_cap_trips_mid_stream` pins.

The change, precisely:

- `httpx.AsyncClient(…)` gains `follow_redirects=False` and `transport=_transport`
  (new keyword-only parameter `_transport=None` on `web_fetch`, threaded through —
  httpx treats None as "the real transport").
- Inside `_get()`, wrap the existing stream block in a hop loop:

```python
            from ptc import policy as _policy
            from .files import _denied
            governed = _policy.file_state() in ("active", "malformed")
            current, hops = url, 0
            while True:
                if governed:
                    _denied("web_fetch", str(current))   # BEFORE the request goes out
                async with c.stream("GET", current) as r:
                    if r.is_redirect:
                        nxt = (str(r.next_request.url) if r.next_request is not None
                               else str(r.headers.get("location")))
                        hops += 1
                        if hops > 10:
                            raise RuntimeError(
                                f"web_fetch: more than 10 redirects from {url}")
                        current = nxt
                        continue
                    # … the EXISTING body/cap/encoding logic runs here, unchanged,
                    # against this final response `r` …
                    return (str(r.url), r.status_code, body, encoding, ctype)
```

  i.e. only the final (non-redirect) response enters the existing `aiter_bytes` cap
  loop; the tuple contract and everything downstream stay byte-identical.
- After the tuple comes back (where `final_url` is in scope, web.py:118-127), add the
  governed audit — one ADDITIONAL line, only under a policy:

```python
        if governed:
            audit.append("web_fetch", url=str(url)[:200], final_url=str(final_url)[:200])
```

  (compute `governed` once in `web_fetch`'s scope so both the loop and this line see
  it). web.py:97's existing unconditional audit line is NOT touched.
- Note on the test file: `test/unit/test_web.py` fakes nothing via httpx — it runs a
  real localhost `http.server` (test_web.py:49-64). The `_transport` seam is NEW and
  conflict-free with that idiom; the new tests use `httpx.MockTransport`.

A malformed policy: `_policy.file_state()` returns "malformed" without raising, so
`governed` is True and the first `_denied` call raises `PolicyError` out of
`policy.match` — loud, exactly the files/bash behavior.

- [ ] **Step 4: Run the web tests**

Run: `uv run pytest test/unit/test_web.py -v`
Expected: PASS (including the file's pre-existing tests — the no-policy path must be
behaviorally identical: same result object, no new audit lines).

- [ ] **Step 5: Commit**

```bash
git add src/ptc/runtime/web.py test/unit/test_web.py
git commit -m "f5(ptc): web_fetch policy is per-hop — checked before each request, both URLs audited (v0.3 i3)"
```

---

### Task 4: Render the `diff:` block

**Files:**
- Modify: `src/ptc/shape.py` (diff block inside the trailing-budget mechanics)
- Test: `test/unit/test_shape.py` (extend), `test/integration/test_mutation_footer.py` (extend)

**Interfaces:**
- Consumes: from Task 2: mutation entries carrying `diff`.
- Produces: `shape.diff_block(mutations: list, budget: int | None) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `test/unit/test_shape.py` (follow its existing footer-test idiom for building
records):

```python
def test_diff_block_renders_write_and_edit_diffs():
    from ptc.shape import diff_block
    muts = [
        {"kind": "edit", "path": "/p/a.py", "diff": "--- /p/a.py\n+++ /p/a.py\n-old\n+new\n"},
        {"kind": "bash", "command": "ls"},
        {"kind": "write", "path": "/p/b.py", "diff": "+++ /p/b.py\n+hello\n"},
    ]
    block = diff_block(muts, budget=None)
    assert block.startswith("diff:")
    assert "-old" in block and "+new" in block and "+hello" in block


def test_diff_block_is_none_without_diffs():
    from ptc.shape import diff_block
    assert diff_block([{"kind": "bash", "command": "ls"}], budget=None) is None
    assert diff_block([], budget=None) is None


def test_diff_block_truncates_honestly_to_budget():
    from ptc.shape import diff_block
    muts = [{"kind": "edit", "path": "/p/a.py", "diff": "x" * 5000}]
    block = diff_block(muts, budget=300)
    assert len(block) <= 300
    assert "audit.jsonl" in block          # the untruncated record's address


def test_render_places_diff_between_result_and_footer(tmp_path):
    from ptc.cells import CellRecord
    from ptc.client import Completed
    from ptc.paths import Config
    from ptc.shape import render
    rec = CellRecord(status="ok", duration_ms=5, result_repr="'done'", error=None,
                     images=[], mutations=[
                         {"kind": "edit", "path": "/p/a.py", "added": 1, "removed": 1,
                          "diff": "--- /p/a.py\n+++ /p/a.py\n-old\n+new\n"}])
    text = render(Completed(3, rec, "body text"), "k", Config.from_env()).text
    assert "diff:" in text and "-old" in text
    # the load-bearing ordering claims:
    assert text.index("result:") < text.index("diff:")
    assert text.index("body text") < text.index("diff:")
    assert "edited /p/a.py" in text or "edit" in text      # footer still fingerprints
```

Append to `test/integration/test_mutation_footer.py` (follow its real-kernel idiom):

```python
def test_kernel_edit_shows_a_diff_block(ptc_home):
    """End-to-end: an in-kernel edit's diff reaches the rendered cell result."""
    from ptc.client import Completed, KernelClient
    from ptc.kernel import ensure_kernel, kill_kernel
    from ptc.paths import Config
    from ptc.shape import render
    cfg = Config.from_env()
    ensure_kernel("diffs", cwd=str(ptc_home), config=cfg)
    kc = KernelClient("diffs")
    target = ptc_home / "sample.txt"
    kc.exec_cell(f"write({str(target)!r}, 'aaa\\nbbb\\n')", timeout_s=60, config=cfg)
    out = kc.exec_cell(f"edit({str(target)!r}, 'bbb', 'ccc')", timeout_s=60, config=cfg)
    assert isinstance(out, Completed)
    text = render(out, "diffs", cfg).text
    assert "diff:" in text and "-bbb" in text and "+ccc" in text
    kill_kernel("diffs")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_shape.py -k diff -v`
Expected: FAIL (`ImportError: cannot import name 'diff_block'`).

- [ ] **Step 3: Implement in `src/ptc/shape.py`**

Beside `footer_line`:

```python
def diff_block(mutations: list, budget: int | None = None) -> str | None:
    """What CHANGED, where the footer says what the cell DID. Bounded like every other
    trailing piece; the untruncated diffs live in audit.jsonl (each already capped at
    2000 chars by the recorder)."""
    parts = [m["diff"] for m in mutations
             if m.get("kind") in ("write", "edit") and m.get("diff")]
    if not parts:
        return None
    text = "diff:\n" + "\n".join(p.rstrip("\n") for p in parts)
    if budget is not None and len(text) > budget:
        note = "\n…[diffs truncated — full (2000-char-capped) copies in audit.jsonl]"
        text = (text[:max(budget - len(note), 0)] + note)[:budget]
    return text
```

In `render`'s Completed branch, the diff joins the trailing set (shape.py:206-222):
compute `d = diff_block(rec.mutations, budget=trailing_budget(cap))` beside `f`, include
`len(d)` in the `trailing` sum, and emit it between the result line and the footer:

```python
    lines.extend(x for x in (err_line, res_line, d, f) if x)
```

- [ ] **Step 4: Run unit + integration**

Run: `uv run pytest test/unit/test_shape.py test/integration/test_mutation_footer.py -v`
Expected: PASS (existing footer/budget tests must keep passing — the diff block obeys
the same discipline they pin; if one sums trailing pieces exhaustively, add `d` to its
expectation).

- [ ] **Step 5: Commit**

```bash
git add src/ptc/shape.py test/unit/test_shape.py test/integration/test_mutation_footer.py
git commit -m "f5(ptc): diff: block in cell results — what changed beside what ran (v0.3 i3)"
```

---

### Task 5: The upgrade-skew gate + governed marker + doctrine

**Files:**
- Modify: `src/ptc/kernel.py` (attach gate), `src/ptc/runtime/bootstrap.py` (governed marker), `src/ptc/cli.py` (catch PolicyGateRefusal), `skills/ptc/SKILL.md` + `README.md` (policy doctrine, one bullet each)
- Test: `test/unit/test_kernel_attach_gates.py` (extend), `test/integration/test_kernel_lifecycle.py` (extend)

**Interfaces:**
- Consumes: from Task 1: `policy.file_state()`, `policy.PolicyGateRefusal`.
- Produces: meta.json gains `governed: true` (written by bootstrap); `kernel._policy_gate(key) -> str | None`.

- [ ] **Step 1: Write the failing unit tests**

Append to `test/unit/test_kernel_attach_gates.py`:

```python
def test_policy_gate_fires_only_for_ungoverned_kernel_with_standing_policy(monkeypatch, tmp_path):
    import json
    import ptc.kernel as kernel
    from ptc.discovery import write_meta
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    write_meta("k", cwd="/x")                     # no `governed` key: a pre-i3 kernel
    assert kernel._policy_gate("k") is None       # no policy file → no gate
    import itertools
    import os as _os
    _tick = itertools.count(1)
    def put(text):
        f = tmp_path / "policy.json"
        f.write_text(text)
        now = __import__("time").time() + next(_tick)   # force a visible mtime step
        _os.utime(f, (now, now))                        # (the cache keys on mtime)
    put(json.dumps({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]}))
    msg = kernel._policy_gate("k")
    assert "restart()" in msg and "remove the policy" in msg
    put('{"version": 1, "deny": []}')
    assert kernel._policy_gate("k") is None       # empty policy does not gate
    put("garbage")
    assert "restart()" in kernel._policy_gate("k")  # malformed gates exactly like active
    write_meta("k", governed=True)
    put(json.dumps({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]}))
    assert kernel._policy_gate("k") is None       # governed kernel attaches under policy
```

Append to `test/integration/test_kernel_lifecycle.py`:

```python
def test_policy_present_refuses_ungoverned_attach_then_recovers(ptc_home):
    """Spec acceptance 6: refusal is an explicit error naming both exits; removing the
    policy re-attaches with the namespace intact."""
    import json

    import pytest
    import ptc.kernel as kernel
    from ptc.client import Completed, KernelClient
    from ptc.discovery import read_meta, write_meta
    from ptc.paths import Config
    from ptc.policy import PolicyGateRefusal
    cfg = Config.from_env()
    kernel.ensure_kernel("gate", cwd=str(ptc_home), config=cfg)
    kc = KernelClient("gate")
    out = kc.exec_cell("kept = 'intact'", timeout_s=60, config=cfg)
    assert isinstance(out, Completed)
    # simulate a pre-governance kernel: strip the governed marker bootstrap wrote
    meta = {k: v for k, v in read_meta("gate").items() if k != "governed"}
    (ptc_home / "kernels" / "gate" / "meta.json").write_text(json.dumps(meta))
    (ptc_home / "policy.json").write_text(json.dumps(
        {"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]}))
    with pytest.raises(PolicyGateRefusal, match="restart\\(\\)"):
        kernel.ensure_kernel("gate", cwd=str(ptc_home), config=cfg)
    (ptc_home / "policy.json").unlink()
    info = kernel.ensure_kernel("gate", cwd=str(ptc_home), config=cfg)
    assert info.spawned is False
    out = kc.exec_cell("print(kept)", timeout_s=60, config=cfg)
    assert "intact" in out.output
    kernel.kill_kernel("gate")


def test_bootstrap_records_the_governed_marker(ptc_home):
    import ptc.kernel as kernel
    from ptc.discovery import read_meta
    from ptc.paths import Config
    kernel.ensure_kernel("marked", cwd=str(ptc_home), config=Config.from_env())
    assert read_meta("marked").get("governed") is True
    kernel.kill_kernel("marked")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest test/unit/test_kernel_attach_gates.py -k policy_gate -v`
Expected: FAIL (`AttributeError: module 'ptc.kernel' has no attribute '_policy_gate'`).

- [ ] **Step 3: Implement**

`src/ptc/kernel.py` — beside the other gates:

```python
def _policy_gate(key: str) -> str | None:
    """Refuse to hand a policy-present user an ungoverned kernel (spec: upgrade skew).

    The wrappers bind at BOOTSTRAP, so a live kernel from a pre-governance build ignores
    whatever policy.json says — a silent bypass wearing a survivability feature's
    clothes. Refusal, not recycle: the namespace may be worth more than the policy, and
    that trade is the USER's — both exits are named. No policy (the default) gates
    nothing; a malformed file gates exactly like an active one (it cannot be believed,
    which is not the same as absent).
    """
    from .policy import file_state
    if file_state() not in ("active", "malformed"):
        return None
    if read_meta(key).get("governed"):
        return None
    from .policy import policy_path
    return (f"kernel {key} predates policy enforcement and a policy file stands at "
            f"{policy_path()} — refusing to attach an ungoverned kernel: restart() to "
            "govern it (namespace is lost), or remove the policy file to keep the "
            "ungoverned namespace")
```

and in `ensure_kernel`'s attach branch, before the return:

```python
        if attachable and stale is None:
            gate = _policy_gate(key)
            if gate is not None:
                from .policy import PolicyGateRefusal
                raise PolicyGateRefusal(gate)
```

`src/ptc/runtime/bootstrap.py::install` — beside the peek wiring belt:

```python
    try:
        from ptc.discovery import write_meta
        write_meta(STATE.key, governed=True)
    except Exception:
        pass   # the marker is a capability record; its absence only costs a gate refusal
```

`src/ptc/cli.py` — `main()` catches it beside `UnknownOwner`:

```python
    except PolicyGateRefusal as e:
        print(f"ptc: {e}", file=sys.stderr)
        return 1
```

(with `from .policy import PolicyGateRefusal` in the imports).

Doctrine — `skills/ptc/SKILL.md`, one bullet beside the trust/audit doctrine:

```
- An optional deny policy at `~/.ptc/policy.json` (env `PTC_POLICY`) trips `bash`,
  `write`, `edit`, and `web_fetch` (per redirect hop) with an audited PermissionError.
  It is the user's file — the plugin ships no rules — and it is a tripwire, not a
  sandbox: raw Python in the kernel remains ungoverned by design (audit over guard
  rails). A malformed file fails governed calls loudly rather than pretending to
  protect.
```

`README.md` — one sentence in its trust-model section mirroring the same claim, AND
amend the existing clause at README.md:45-46 ("give visibility, not enforcement") so the
section does not argue with itself — e.g. "give visibility, not enforcement — plus an
opt-in deny tripwire (`~/.ptc/policy.json`), which is a tripwire, never a sandbox". Keep
the README/skill wording aligned as that section's convention demands.

Also add one CLI test beside `test/unit/test_cli_commands.py`'s existing
`test_an_unidentifiable_owner_is_a_sentence_not_a_traceback` idiom:

```python
def test_policy_gate_refusal_is_a_sentence_not_a_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setenv("PTC_SESSION", "gatecli")
    import ptc.cli as cli
    from ptc.policy import PolicyGateRefusal
    def boom(*a, **kw):
        raise PolicyGateRefusal("kernel gatecli predates policy enforcement — restart() or remove the policy")
    monkeypatch.setattr(cli, "ensure_kernel", boom)
    assert cli.main(["exec", "1+1"]) == 1
    err = capsys.readouterr().err
    assert "restart()" in err and "Traceback" not in err
```

- [ ] **Step 4: Run everything named**

Run: `uv run pytest test/unit/test_kernel_attach_gates.py test/integration/test_kernel_lifecycle.py test/unit/test_cli_commands.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ptc/kernel.py src/ptc/runtime/bootstrap.py src/ptc/cli.py skills/ptc/SKILL.md README.md test/unit/test_kernel_attach_gates.py test/integration/test_kernel_lifecycle.py
git commit -m "f5(ptc): policy-present ungoverned kernels refuse attach with both exits named; bootstrap records governed (v0.3 i3)"
```

---

### Task 6: Final verification — the spec's acceptance, as written

**Files:** none (commands only; no commits).

The spec's acceptance for initiative 3 (quoted verbatim):

> 1. A policy denying `^rm ` → `bash("rm -rf /tmp/x")` raises PermissionError naming rule 0;
>    audit.jsonl gains a `denied` line; `bash("echo ok")` unaffected.
> 2. `edit()` on a file → the shaped result carries a `diff:` block with `-`/`+` lines; the
>    audit.jsonl entry holds the same diff (≤ 2000 chars).
> 3. No policy file → the full existing suite passes unchanged.
> 4. Malformed policy.json → `bash("echo hi")` raises naming the JSON error; `read()` works.
> 5. A deny rule on a destination host, requested through an allowed redirector that 302s to
>    it → `web_fetch(redirector)` raises at the hop; audit records both URLs.
> 6. A live pre-governance kernel plus a policy file with one deny rule → attach fails with
>    the two-exit error; removing the policy file re-attaches with the namespace intact.

- [ ] **Step 1: Full suite (this IS criterion 3 — no policy file exists in the test env)**

Run: `uv run pytest test/ -q` — paste the tail.

- [ ] **Step 2: The other five criteria via their named twins**

Run:
`uv run pytest "test/unit/test_governed_wrappers.py::test_denied_bash_raises_before_spawn" "test/unit/test_governed_wrappers.py::test_unmatched_calls_run_untouched" "test/integration/test_mutation_footer.py::test_kernel_edit_shows_a_diff_block" "test/unit/test_governed_wrappers.py::test_malformed_policy_is_loud_for_governed_and_silent_for_read" "test/unit/test_web.py::test_web_fetch_denies_at_the_hop_not_after_it" "test/unit/test_web.py::test_web_fetch_follows_redirects_and_audits_both_urls_under_policy" "test/integration/test_kernel_lifecycle.py::test_policy_present_refuses_ungoverned_attach_then_recovers" -v`

Expected: all pass. Map in the report: criterion 1 → denied_bash + unmatched (which now runs `bash("echo ok")` itself); 2 → kernel_edit_shows_a_diff_block (also assert from its output that the audit line's diff is ≤2100 chars — read the audit.jsonl the test's ptc_home leaves if the fixture allows, else cite the cap test `test_huge_diff_is_capped_with_a_note`); 4 → malformed_loud; 5 → the two web tests (note: the denial is at the second hop — the redirector itself was allowed — and the audit-both-URLs half is proven by the non-denied twin, since a denied fetch never completes to have a final URL); 6 → refuses_ungoverned_attach_then_recovers.

- [ ] **Step 3: Report**

Suite tail; per-criterion PASS/FAIL with test names; any deviation stated plainly.

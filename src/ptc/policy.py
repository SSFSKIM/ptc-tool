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
    a silently ignored typo would leave the user thinking themselves protected. A file
    present but unreadable raises this too: cannot be read is a case of cannot be believed."""


class PolicyGateRefusal(RuntimeError):
    """The attach gate's refusal (kernel predates enforcement while a policy stands).
    Its own type so the CLI can print it as a sentence rather than a traceback."""


def policy_path() -> Path:
    raw = os.environ.get("PTC_POLICY")
    return Path(raw).expanduser() if raw else ptc_home() / "policy.json"


#: The tools whose wrappers consult the policy. A name outside this set can never match,
#: so accepting one would load protection incapable of firing: parse fails loudly instead.
GOVERNED_TOOLS = ("bash", "write", "edit", "web_fetch")

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
        if not raw["tools"]:
            raise PolicyError(f"deny[{i}] \"tools\" must name at least one tool")
        for t in raw["tools"]:
            if t not in GOVERNED_TOOLS:
                raise PolicyError(f"deny[{i}] \"tools\" names an ungoverned tool "
                                  f"\"{t}\" (must be one of {', '.join(GOVERNED_TOOLS)})")
        pattern, path = raw.get("pattern"), raw.get("path")
        if (pattern is None) == (path is None):
            raise PolicyError(f"deny[{i}] needs exactly one of \"pattern\" or \"path\"")
        if pattern is not None and not isinstance(pattern, str):
            raise PolicyError(f"deny[{i}] \"pattern\" must be a string")
        if path is not None and not isinstance(path, str):
            raise PolicyError(f"deny[{i}] \"path\" must be a string")
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
    try:
        text = p.read_text()
    except OSError as e:
        raise PolicyError(f"policy.json cannot be read: {e}") from e
    rules = _parse(text)
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

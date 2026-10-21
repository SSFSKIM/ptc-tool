"""Prompt profiles: what a child's prompt is made of.

A profile has four axes — `system`, `tools`, `memory`, `settings` — and four presets
(`bare`, `reader`, `worker`, `full`). Explicit keywords override a preset's fields:
`agent.run(t, profile="reader", tools=["Read"])`. How a resolved profile becomes a CLI
child's options is `claude_backend._sdk_options`; this module only says what the profile is.

Tool definitions are what a child's prompt costs (the agent-host spec's measurements: the
default tool set is ~17k tokens, Claude Code's whole system prompt ~2.7k), so a profile
names its tool list exactly and takes Claude Code's system prompt whole or not at all.
"""
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Literal

SystemSpec = str | Literal["claude_code"] | tuple[Literal["claude_code"], str]

#: `llm()`'s system prompt since before profiles existed.
LLM_SYSTEM = "Answer directly and concisely. No preamble."

#: Short on purpose: every word is paid by every reader child, on every request.
READER_SYSTEM = ("Investigate using only the tools given, and change nothing. Cite each "
                 "file you rely on by its absolute path. End with your findings.")

_MEMORY = ("global", "project", "both")
_SETTINGS = ("none", "user", "all")


@dataclass(frozen=True)
class Profile:
    system: SystemSpec
    tools: list[str] | Literal["default"]
    memory: Literal["global", "project", "both"] | None = None
    settings: Literal["none", "user", "all"] = "none"

    def __post_init__(self):
        s = self.system
        if not (isinstance(s, str) or (isinstance(s, tuple) and len(s) == 2
                                       and s[0] == "claude_code" and isinstance(s[1], str))):
            raise ValueError(f"profile system must be a string, 'claude_code' or "
                             f"('claude_code', append), got {s!r}")
        t = self.tools
        if not (t == "default" or (isinstance(t, list) and all(isinstance(n, str) for n in t))):
            raise ValueError(f"profile tools must be 'default' or a list of tool names, "
                             f"got {t!r}")
        if self.memory is not None and self.memory not in _MEMORY:
            raise ValueError(f"profile memory must be None or one of {_MEMORY}, "
                             f"got {self.memory!r}")
        if self.settings not in _SETTINGS:
            raise ValueError(f"profile settings must be one of {_SETTINGS}, "
                             f"got {self.settings!r}")


PRESETS: dict[str, Profile] = {
    "bare": Profile(LLM_SYSTEM, []),
    "reader": Profile(READER_SYSTEM, ["Read", "Grep", "Glob"]),
    "worker": Profile("claude_code", ["Read", "Grep", "Glob", "Bash", "Edit", "Write"],
                      memory="global"),
    # Today's agent child, unchanged: an EMPTY system prompt (the SDK sends
    # `--system-prompt ""` when none is set, so the child has never had Claude Code's own)
    # and Claude Code's whole tool set.
    "full": Profile("", "default"),
}

_FIELDS = tuple(f.name for f in fields(Profile))


def resolve(profile: str | Profile | None, *, default: str, **overrides) -> Profile:
    """The profile a call runs: a preset by name, a `Profile` as given, or `default`'s preset
    when `None` — with every override that is not `None` laid over it.

    `None` means "not given" because the keywords that feed this (`system=`, `tools=`)
    default to `None` on every call; to clear a preset's memory, pass a `Profile`.
    The tool list is always a fresh copy, so no caller can edit a preset through it.
    """
    bad = set(overrides) - set(_FIELDS)
    if bad:
        raise TypeError(f"not profile fields: {sorted(bad)}; use one of {list(_FIELDS)}")
    if profile is None:
        profile = default
    if isinstance(profile, str):
        if profile not in PRESETS:
            raise ValueError(f"unknown profile {profile!r}; use one of {sorted(PRESETS)} "
                             "or a Profile")
        profile = PRESETS[profile]
    elif not isinstance(profile, Profile):
        raise TypeError(f"profile must be a preset name or a Profile, got {profile!r}")
    given = {k: v for k, v in overrides.items() if v is not None}
    if isinstance(given.get("tools"), tuple):
        given["tools"] = list(given["tools"])
    p = replace(profile, **given)
    return replace(p, tools=list(p.tools)) if isinstance(p.tools, list) else p


_GLOBAL_LABEL = "user's private global instructions for all projects"
_PROJECT_LABEL = "project instructions, checked into the codebase"


def _framed(path: Path, label: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None                      # missing or unreadable: contributes nothing
    if not text.strip():
        return None
    return f"Contents of {path} ({label}):\n\n{text}"


def _project_files(cwd: str) -> list[Path]:
    """Every CLAUDE.md from the git root down to `cwd`, root first — Claude Code's order.

    The git root is the nearest ancestor holding `.git` (a directory, or a linked
    worktree's file). Outside git the walk is `cwd` alone: nothing above it is project.
    """
    here = Path(cwd).absolute()
    chain = [here, *here.parents]
    root = next((d for d in chain if (d / ".git").exists()), here)
    dirs = chain[:chain.index(root) + 1]
    return [d / "CLAUDE.md" for d in reversed(dirs)]


def memory_text(memory: str | None, cwd: str) -> str:
    """The memory files' text in Claude Code's own framing, `""` when nothing applies.

    `"global"` is `~/.claude/CLAUDE.md`; `"project"` every CLAUDE.md from `cwd` up to its
    git root, root first; `"both"` the global file, then the project files.
    """
    if memory is None:
        return ""
    if memory not in _MEMORY:
        raise ValueError(f"memory must be None or one of {_MEMORY}, got {memory!r}")
    parts = []
    if memory in ("global", "both"):
        parts.append(_framed(Path.home() / ".claude" / "CLAUDE.md", _GLOBAL_LABEL))
    if memory in ("project", "both"):
        parts.extend(_framed(p, _PROJECT_LABEL) for p in _project_files(cwd))
    return "\n\n".join(p for p in parts if p)

"""Prompt profiles: presets, keyword overrides, memory text, and how a resolved profile
becomes a CLI child's options — no SDK call, no auth, no network.

The `--bare` rule and the setting-sources mapping are read off the argv the SDK's own
transport builds from `_sdk_options`, so what is pinned is what the CLI is actually given.
"""
import asyncio
import json

import pytest

from ptc.runtime import claude_backend, profiles
from ptc.runtime.agents import AgentOpts, AgentResult, _Agent
from ptc.runtime.profiles import PRESETS, Profile, memory_text, resolve
from ptc.runtime.state import STATE


# -- presets -------------------------------------------------------------------
def test_preset_field_values():
    assert set(PRESETS) == {"bare", "reader", "worker", "full"}
    assert PRESETS["bare"] == Profile("Answer directly and concisely. No preamble.", [],
                                      None, "none")
    r = PRESETS["reader"]
    assert r.system == profiles.READER_SYSTEM and r.tools == ["Read", "Grep", "Glob"]
    assert r.memory is None and r.settings == "none"
    assert PRESETS["worker"] == Profile(
        "claude_code", ["Read", "Grep", "Glob", "Bash", "Edit", "Write"], "global", "none")
    # Today's agent child: the SDK sends `--system-prompt ""` for an unset system, so the
    # default child has an EMPTY system prompt and Claude Code's whole tool set.
    assert PRESETS["full"] == Profile("", "default", None, "none")


def test_reader_system_is_short_and_read_only():
    words = profiles.READER_SYSTEM.split()
    assert 0 < len(words) < 120
    assert "absolute" in profiles.READER_SYSTEM.lower()


def test_resolve_names_defaults_and_instances():
    assert resolve(None, default="full") == PRESETS["full"]
    assert resolve("reader", default="full") == PRESETS["reader"]
    custom = Profile("be terse", ["Read"])
    assert resolve(custom, default="full") == custom
    with pytest.raises(ValueError, match="unknown profile 'nope'"):
        resolve("nope", default="full")


def test_keyword_overrides_win_and_none_means_not_given():
    p = resolve("reader", default="full", tools=["Read"])
    assert p.tools == ["Read"] and p.system == profiles.READER_SYSTEM
    p = resolve(None, default="full", system="be terse", tools=None)
    assert p.system == "be terse" and p.tools == "default"
    p = resolve("worker", default="full", memory="both", settings="user")
    assert p.memory == "both" and p.settings == "user"
    with pytest.raises(TypeError, match="colour"):
        resolve("bare", default="full", colour="red")


def test_resolved_tool_lists_never_alias_a_preset():
    p = resolve("reader", default="full")
    p.tools.append("Bash")
    assert PRESETS["reader"].tools == ["Read", "Grep", "Glob"]


@pytest.mark.parametrize("kw, match", [
    ({"system": 3, "tools": []}, "system"),
    ({"system": ("other", "x"), "tools": []}, "system"),
    ({"system": "x", "tools": "all"}, "tools"),
    ({"system": "x", "tools": ["Read", 1]}, "tools"),
    ({"system": "x", "tools": [], "memory": "home"}, "memory"),
    ({"system": "x", "tools": [], "settings": "project"}, "settings"),
])
def test_profile_refuses_values_outside_its_axes(kw, match):
    with pytest.raises(ValueError, match=match):
        Profile(**kw)


# -- memory text ---------------------------------------------------------------
GLOBAL = "user's private global instructions for all projects"
PROJECT = "project instructions, checked into the codebase"


def _tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("global rules")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("root rules")
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "pkg" / "sub" / "CLAUDE.md").write_text("sub rules")
    (tmp_path / "CLAUDE.md").write_text("outside the repo")   # above the git root
    return home, repo


def test_memory_text_framing_and_order(tmp_path, monkeypatch):
    home, repo = _tree(tmp_path, monkeypatch)
    cwd = str(repo / "pkg" / "sub")
    g = f"Contents of {home}/.claude/CLAUDE.md ({GLOBAL}):\n\nglobal rules"
    root = f"Contents of {repo}/CLAUDE.md ({PROJECT}):\n\nroot rules"
    sub = f"Contents of {repo}/pkg/sub/CLAUDE.md ({PROJECT}):\n\nsub rules"
    assert memory_text(None, cwd) == ""
    assert memory_text("global", cwd) == g
    # root first, the cwd's own last, and nothing above the git root
    assert memory_text("project", cwd) == f"{root}\n\n{sub}"
    assert memory_text("both", cwd) == f"{g}\n\n{root}\n\n{sub}"


def test_memory_text_missing_files_contribute_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    bare = tmp_path / "plain"
    bare.mkdir()
    assert memory_text("both", str(bare)) == ""


def test_memory_text_a_worktree_root_is_its_git_root(tmp_path, monkeypatch):
    """A linked worktree's `.git` is a FILE, not a directory; it still bounds the walk."""
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    (tmp_path / "CLAUDE.md").write_text("main checkout")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    (wt / "CLAUDE.md").write_text("worktree rules")
    assert memory_text("project", str(wt)) == \
        f"Contents of {wt}/CLAUDE.md ({PROJECT}):\n\nworktree rules"


def test_memory_text_follows_a_symlinked_cwd_to_its_repo(tmp_path, monkeypatch):
    """A cwd reached through a symlink walks its target's ancestors, not the link's."""
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("repo rules")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "CLAUDE.md").write_text("pkg rules")
    links = tmp_path / "links"
    links.mkdir()
    (links / "CLAUDE.md").write_text("beside the link")
    (links / "here").symlink_to(repo / "pkg")
    real = repo.resolve()
    assert memory_text("project", str(links / "here")) == (
        f"Contents of {real}/CLAUDE.md ({PROJECT}):\n\nrepo rules\n\n"
        f"Contents of {real}/pkg/CLAUDE.md ({PROJECT}):\n\npkg rules")


def test_memory_text_outside_git_reads_the_cwd_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    (tmp_path / "CLAUDE.md").write_text("parent")
    d = tmp_path / "d"
    d.mkdir()
    (d / "CLAUDE.md").write_text("here")
    assert memory_text("project", str(d)) == f"Contents of {d}/CLAUDE.md ({PROJECT}):\n\nhere"


# -- the CLI route: a resolved profile as the child's argv ---------------------
def _argv(o: AgentOpts, **kw) -> list[str]:
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )
    t = SubprocessCLITransport(prompt="x", options=claude_backend._sdk_options(o, **kw))
    t._cli_path = "claude"
    return t._build_command()


def _flag(argv, name):
    i = argv.index(name)
    return argv[i + 1]


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "depth": 0, "max_depth": 1, "cwd": str(tmp_path)}
    monkeypatch.delenv("PTC_LAUNCHER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-marker")


@pytest.fixture
def no_creds(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def test_default_opts_build_todays_child(kernel, creds):
    """`full` is today's agent child: empty system prompt, the CLI's default tool set (no
    `--tools` at all), no settings, and no `--bare` — `--bare` narrows a default tool set."""
    argv = _argv(AgentOpts())
    assert _flag(argv, "--system-prompt") == ""
    assert "--tools" not in argv and "--bare" not in argv
    assert "--setting-sources=" in argv
    argv = _argv(AgentOpts(system="be terse"))
    assert _flag(argv, "--system-prompt") == "be terse" and "--bare" not in argv


def test_bare_rule_with_credentials_in_the_environment(kernel, creds):
    assert "--bare" in _argv(AgentOpts(profile=PRESETS["bare"]), bare_llm=True)
    assert "--bare" in _argv(AgentOpts(profile=Profile("x", [])))


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_either_credential_variable_turns_bare_on(kernel, no_creds, monkeypatch, var):
    monkeypatch.setenv(var, "set")
    assert "--bare" in _argv(AgentOpts(profile=PRESETS["bare"]), bare_llm=True)


def test_bare_rule_without_credentials(kernel, no_creds):
    assert "--bare" not in _argv(AgentOpts(profile=PRESETS["bare"]), bare_llm=True)
    assert "--bare" not in _argv(AgentOpts(profile=Profile("x", [])))


def test_an_empty_credential_is_no_credential(kernel, no_creds, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    assert "--bare" not in _argv(AgentOpts(profile=PRESETS["bare"]), bare_llm=True)


@pytest.mark.parametrize("profile", [
    PRESETS["reader"],                      # --bare would cut Read/Grep/Glob to Read
    PRESETS["worker"],                      # ...and swap claude_code for a stub prompt
    PRESETS["full"],                        # ...and cut the default set to Bash/Edit/Read
    Profile("x", ["Read"]),
    Profile(("claude_code", "more"), []),
    Profile("x", [], settings="user"),
])
def test_bare_is_never_added_where_it_would_change_the_child(kernel, creds, profile):
    assert "--bare" not in _argv(AgentOpts(profile=profile))


def test_settings_map_to_setting_sources(kernel, no_creds):
    def sources(settings):
        return claude_backend._sdk_options(
            AgentOpts(profile=Profile("x", ["Read"], settings=settings))).setting_sources
    assert sources("none") == []
    assert sources("user") == ["user"]
    assert sources("all") == ["user", "project", "local"]


def test_system_axis_maps_to_string_or_preset(kernel, no_creds):
    def sp(system, memory=None):
        return claude_backend._sdk_options(
            AgentOpts(profile=Profile(system, ["Read"], memory))).system_prompt
    assert sp("be terse") == "be terse"
    assert sp("claude_code") == {"type": "preset", "preset": "claude_code"}
    assert sp(("claude_code", "also this")) == \
        {"type": "preset", "preset": "claude_code", "append": "also this"}


def test_tools_axis(kernel, no_creds):
    def tools(t):
        return claude_backend._sdk_options(AgentOpts(profile=Profile("x", t))).tools
    assert tools([]) == []
    assert tools(["Read", "Grep"]) == ["Read", "Grep"]
    assert tools("default") is None


def test_memory_is_appended_to_whatever_system_the_profile_has(kernel, no_creds):
    home = kernel / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("global rules")
    mem = f"Contents of {home}/.claude/CLAUDE.md ({GLOBAL}):\n\nglobal rules"

    def sp(system):
        return claude_backend._sdk_options(
            AgentOpts(profile=Profile(system, [], "global"))).system_prompt
    assert sp("be terse") == f"be terse\n\n{mem}"
    assert sp("") == mem
    assert sp("claude_code") == {"type": "preset", "preset": "claude_code", "append": mem}
    assert sp(("claude_code", "more")) == \
        {"type": "preset", "preset": "claude_code", "append": f"more\n\n{mem}"}


def test_project_memory_is_read_from_the_childs_cwd(kernel, no_creds):
    repo = kernel / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("repo rules")
    o = AgentOpts(profile=Profile("x", [], "project"), cwd=str(repo))
    assert claude_backend._sdk_options(o).system_prompt == \
        f"x\n\nContents of {repo}/CLAUDE.md ({PROJECT}):\n\nrepo rules"


LAUNCHER = "/opt/ptc/bin/ptc-launch"


def _ptc_server(argv) -> bool:
    if "--mcp-config" not in argv:
        return False
    return json.loads(_flag(argv, "--mcp-config"))["mcpServers"]["ptc"]["command"] == LAUNCHER


def test_llm_never_gets_the_ptc_server(kernel, creds, monkeypatch):
    monkeypatch.setenv("PTC_LAUNCHER", LAUNCHER)
    o = claude_backend._sdk_options(AgentOpts(profile=PRESETS["bare"]), bare_llm=True)
    assert o.tools == [] and o.max_turns == 1 and not o.mcp_servers
    # bare_llm holds even for a profile that names the server
    argv = _argv(AgentOpts(profile=Profile("x", ["mcp__ptc"])), bare_llm=True)
    assert not _ptc_server(argv) and _flag(argv, "--tools") == ""


@pytest.mark.parametrize("tools, server, builtins", [
    ("default", True, None),                       # today's child keeps it
    (["Read", "Grep", "Glob"], False, "Read,Grep,Glob"),
    ([], False, ""),
    (["Read", "mcp__ptc"], True, "Read"),            # the whole server, by name
    (["Read", "mcp__ptc__exec"], True, "Read"),      # one of its tools names it too
    (["mcp__ptcx"], False, "mcp__ptcx"),             # a different server's prefix
])
def test_ptc_server_only_when_the_tools_name_it(kernel, no_creds, monkeypatch,
                                                 tools, server, builtins):
    monkeypatch.setenv("PTC_LAUNCHER", LAUNCHER)
    argv = _argv(AgentOpts(profile=Profile("x", tools)))
    assert _ptc_server(argv) is server
    if builtins is None:
        assert "--tools" not in argv
    else:
        assert _flag(argv, "--tools") == builtins


def test_reader_and_default_agent_through_opts(kernel, no_creds, monkeypatch):
    monkeypatch.setenv("PTC_LAUNCHER", LAUNCHER)
    assert _ptc_server(_argv(AgentOpts()))
    assert not _ptc_server(_argv(AgentOpts(profile=PRESETS["reader"])))
    assert not _ptc_server(_argv(AgentOpts(tools=["Read"])))


GIT_OFF = "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"


@pytest.mark.parametrize("o, bare_llm, off", [
    (AgentOpts(), False, False),                                   # full: today's child
    (AgentOpts(profile=PRESETS["full"]), False, False),
    (AgentOpts(profile=PRESETS["bare"]), True, True),
    (AgentOpts(profile=PRESETS["reader"]), False, True),
    (AgentOpts(profile=PRESETS["worker"]), False, True),
    (AgentOpts(system="be terse"), False, True),                   # not full any more
])
def test_git_context_is_off_for_every_profile_but_full(kernel, no_creds, o, bare_llm, off):
    env = claude_backend._sdk_options(o, bare_llm=bare_llm).env
    assert (env.get(GIT_OFF) == "1") is off
    assert off or GIT_OFF not in env



TITLE_OFF = "CLAUDE_CODE_DISABLE_TERMINAL_TITLE"


@pytest.mark.parametrize("o, bare_llm", [
    (AgentOpts(), False),                                          # full, today's child too
    (AgentOpts(profile=PRESETS["bare"]), True),
    (AgentOpts(profile=PRESETS["reader"]), False),
    (AgentOpts(profile=PRESETS["worker"]), False),
])
def test_no_cli_child_asks_for_a_session_title(kernel, no_creds, o, bare_llm):
    """CLI 2.1.305 names an SDK-launched session with a second model request (~1,200
    tokens) after its first prompt; `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` latches that off
    (the bundle's `Sfs()`), and nothing else a child carries does."""
    assert claude_backend._sdk_options(o, bare_llm=bare_llm).env[TITLE_OFF] == "1"

def test_the_bare_decision_audits_and_logs_nothing(kernel, monkeypatch, capsys):
    """The trust note in `_child_env`: the merged environment is read, never recorded."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-secret-value")
    STATE.cell_mutations = []
    o = claude_backend._sdk_options(AgentOpts(profile=PRESETS["bare"]), bare_llm=True)
    assert "sk-ant-oat-secret-value" not in repr(o.env) and not STATE.cell_mutations
    assert "sk-ant-oat-secret-value" not in capsys.readouterr().out


# -- agent.* ---------------------------------------------------------------------
class _Recorder:
    def __init__(self):
        self.opts = []

    async def run_once(self, task, o, *, resume=None, fork=False):
        self.opts.append(o)
        return AgentResult("ok", "s", None, 0.0, 1, 1)


def _agent(tmp_path):
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 2}
    rec, codex = _Recorder(), _Recorder()
    return _Agent(STATE.config, {"claude": rec, "codex": codex}), rec, codex


def test_agent_opts_resolve_profile_with_keywords_overriding(tmp_path):
    a, rec, _ = _agent(tmp_path)
    asyncio.run(a.run("t"))
    asyncio.run(a.run("t", profile="reader", tools=["Read"]))
    asyncio.run(a.run("t", system="be terse"))
    full, reader, terse = rec.opts
    assert full.profile == PRESETS["full"]
    assert reader.profile == Profile(profiles.READER_SYSTEM, ["Read"])
    assert terse.profile == Profile("be terse", "default")


def test_codex_refuses_a_non_default_profile(tmp_path):
    a, _, codex = _agent(tmp_path)
    with pytest.raises(ValueError, match="profile"):
        asyncio.run(a.run("t", provider="codex", profile="reader"))
    with pytest.raises(ValueError, match="profile"):
        a.spawn("t", provider="codex", profile=Profile("x", []))
    asyncio.run(a.run("t", provider="codex", profile="full"))
    asyncio.run(a.run("t", provider="codex", system="be terse"))
    assert [o.system for o in codex.opts] == [None, "be terse"]
    assert all(o.profile is None for o in codex.opts)


def test_unknown_profile_name_is_a_value_error(tmp_path):
    a, _, _ = _agent(tmp_path)
    with pytest.raises(ValueError, match="unknown profile"):
        asyncio.run(a.run("t", profile="writer"))


# -- llm() -----------------------------------------------------------------------
def test_llm_runs_the_bare_profile_and_overrides_its_system(tmp_path, monkeypatch):
    from ptc.runtime import _llm as llm_mod
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    seen = []

    async def fake_run_once(task, o, *, resume=None, fork=False, bare_llm=False):
        seen.append((o.profile, bare_llm))
        return AgentResult("ok", "s", None, 0, 1, 5)
    monkeypatch.setattr(llm_mod, "_run_once", fake_run_once)
    asyncio.run(llm_mod.llm("x"))
    asyncio.run(llm_mod.llm("x", system="be terse"))
    asyncio.run(llm_mod.llm("x", profile=Profile("judge strictly", [], "global")))
    assert seen == [(PRESETS["bare"], True),
                    (Profile("be terse", []), True),
                    (Profile("judge strictly", [], "global"), True)]


@pytest.mark.parametrize("profile", ["reader", "full", Profile("x", ["Read"])])
def test_llm_refuses_a_profile_with_tools(tmp_path, monkeypatch, profile):
    from ptc.runtime import _llm as llm_mod
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}

    async def never(*a, **k):
        raise AssertionError("no child may start")
    monkeypatch.setattr(llm_mod, "_run_once", never)
    with pytest.raises(ValueError, match="tools"):
        asyncio.run(llm_mod.llm("x", profile=profile))


def test_llm_refuses_a_profile_that_loads_user_settings(tmp_path, monkeypatch):
    """The user's settings bring their plugins and MCP servers — ptc's own among them —
    which would undo `bare_llm`'s no-tools, no-ptc-server guarantee."""
    from ptc.runtime import _llm as llm_mod
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "k", "max_concurrency": 8}
    with pytest.raises(ValueError, match="settings"):
        asyncio.run(llm_mod.llm("x", profile=Profile("x", [], settings="user")))

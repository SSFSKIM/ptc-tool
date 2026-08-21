# S5: MCP image block rendering

**Question.** Does Claude Code render an `ImageContent` block returned by an MCP tool?
Spec criteria: *Promote* → plots visible inline. *Fallback* → text mentions the saved PNG
path only.

This spike is a live `claude` invocation, not a pytest case: the thing under test is the
host's rendering of an MCP `CallToolResult`, which only the real client can answer. The
runbook is the instrument; re-running it verbatim reproduces the verdict.

## Cell under test

```python
import matplotlib; matplotlib.use('module://matplotlib_inline.backend_inline')
import matplotlib.pyplot as plt
plt.plot([1,4,2,8]); plt.title('s5'); plt.show()
print('S5_PLOTTED')
```

`plt.show()` on the inline backend publishes `display_data` with `image/png`; the kernel's
display shim (`ptc.runtime.bootstrap._install_display_shim`) writes it to
`~/.ptc/kernels/<key>/cells/<n>-0.png` and records the path, and `ptc.mcp._content` turns it
into an MCP image content block after the text block.

## 1. Substrate check (keyless, no billing)

```bash
cd ptc-surface/ptc
PTC_SESSION=s5-local ~/.ptc/venv/bin/ptc exec "<cell under test>"
ls ~/.ptc/kernels/s5-local/cells/          # expect 2-0.png (or <n>-0.png)
~/.ptc/venv/bin/ptc kill -s s5-local
```

The PNG must land on disk regardless of the verdict — that file is the fallback.

## 2. Headless evidence (primary)

Plugin-provided tools are named `mcp__plugin_ptc_ptc__<tool>` and arrive **deferred behind
ToolSearch** (spike S3), so the prompt must name the long form and permit a ToolSearch load.
Strip `CLAUDE_CODE_SESSION_ID` / `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` so the nested
session does not key its kernel to the enclosing one. Subscription auth only — never set
`ANTHROPIC_API_KEY`.

```bash
cd /tmp/ptc-s5-scratch
env -u CLAUDE_CODE_SESSION_ID -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
  claude -p --plugin-dir <repo>/ptc-surface/ptc/plugin \
    --permission-mode bypassPermissions \
    --output-format stream-json --verbose \
    "Call the mcp__plugin_ptc_ptc__exec tool (load its schema with ToolSearch first if
     it is not loaded) with exactly this code: <cell under test>
     Then reply with just: DONE" > s5-stream.json
```

Read `s5-stream.json`: for every `user` message, inspect each `tool_result` block's
`content` array. **The question is whether a block with `"type": "image"` and base64 `data`
survives into the transcript the model sees**, or whether the host dropped/replaced it.
Also record the `tool_use` names actually emitted (evidence of the deferred-tool path) and
what the assistant *says* about the image.

## 3. Interactive observation (secondary, best-effort)

```bash
tmux new-session -d -s ptc-s5 -x 200 -y 50 \
  "cd <repo>/ptc-surface/ptc && claude --plugin-dir ./plugin"
tmux send-keys -t ptc-s5 '<the prompt>' Enter
tmux capture-pane -p -t ptc-s5        # look for an image indicator in the transcript
tmux kill-session -t ptc-s5
```

A terminal cannot show pixels, so "visible inline" here means the transcript acknowledges an
image block (e.g. a `[Image]` placeholder in the tool result) rather than only text. Record
what was actually on the pane; do not infer rendering that was not seen.

## 4. Verdict

- **Promote** — an image content block reaches the model/transcript: keep `_content` as
  built in T8 (image block + PNG on disk).
- **Fallback** — the block is dropped, errors, or never reaches the transcript: `_content`
  stops emitting `ImageContent` and appends `[image saved: <path>]` instead.

Either way append the observation to the spec's *Surprises & Discoveries* and note the
Claude Code version actually run (`claude --version`).

## 5. Cleanup

Kill only the kernels these runs created (`~/.ptc/kernels/<key>/owner.json` pid), and only
the tmux session created above.

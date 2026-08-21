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
    "Call the mcp__plugin_ptc_ptc__exec tool (ToolSearch its schema first if it is not
     loaded) with exactly this code: <cell under test, semicolon-joined on one line>
     -- then say in one sentence what the tool result contained, and reply DONE" \
  > s5-stream.json
```

Read `s5-stream.json`: for every `user` message, inspect each `tool_result` block's
`content` array. **The question is whether a block with `"type": "image"` and base64 `data`
survives into the transcript the model sees**, or whether the host dropped/replaced it.
Decide it on the wire, in this order:

1. the block's shape — `{"type":"image","source":{"type":"base64","media_type":"image/png","data":…}}`;
2. its position — after the text block, matching `_content`'s order;
3. `is_error` unset on the `tool_result`;
4. **byte identity** — base64-decode `data`, sha256 it, and compare against
   `~/.ptc/kernels/<session>/cells/2-0.png`. A length match is weaker; hash the bytes.

Also record the `tool_use` names actually emitted (evidence of the deferred-tool path).

The assistant's one-sentence answer is recorded, but it is **not** evidence that the model
read pixels: the plot title and the data points are both in the code the model itself
submitted, so its description is derivable from the tool input alone. A prompt that would
actually test pixel reading has to ask for something only the rendering carries (line
colour, y-axis tick values, gridline count) or plot data absent from the submitted code.

## 3. Interactive observation (secondary, best-effort)

```bash
tmux new-session -d -s ptc-s5 -x 180 -y 50 \
  "cd <repo>/ptc-surface/ptc && env -u CLAUDE_CODE_SESSION_ID -u CLAUDECODE \
   -u CLAUDE_CODE_ENTRYPOINT claude --plugin-dir ./plugin \
   --permission-mode bypassPermissions"

# Two startup dialogs stand between the launch and the REPL. Capture the pane, answer
# each, and only then send the prompt:
#   1. folder-trust ("Do you trust the files in this folder?") in an untrusted cwd;
#   2. the bypass-permissions acceptance screen (--permission-mode bypassPermissions).
tmux capture-pane -p -t ptc-s5        # confirm which dialog is showing before answering
tmux send-keys -t ptc-s5 Enter        # per dialog, once its selection is correct

# The prompt is multi-line and contains quotes; send-keys mangles it. Paste it:
tmux load-buffer -t ptc-s5 <(printf '%s' "<the prompt>")
tmux paste-buffer -t ptc-s5
tmux send-keys -t ptc-s5 Enter

tmux capture-pane -p -t ptc-s5        # collapsed view
# then ctrl+O for the detailed transcript, and capture again:
tmux send-keys -t ptc-s5 C-o
tmux capture-pane -p -t ptc-s5
tmux kill-session -t ptc-s5           # kill only the session created here
```

A terminal cannot show pixels, so "visible inline" here means the transcript acknowledges an
image block (e.g. a `[Image]` placeholder in the tool result) rather than only text. Record
what was actually on the pane; do not infer rendering that was not seen. Observed on 2.1.238:
the collapsed view shows only `Called plugin:ptc:ptc` with no image indicator at all, and the
ctrl+O detail view shows the text block plus a second `⎿ [Image]` row.

## 4. Verdict

- **Promote** — an image content block reaches the model/transcript: keep `_content` as
  built in T8 (image block + PNG on disk).
- **Fallback** — the block is dropped, errors, or never reaches the transcript: `_content`
  stops emitting `ImageContent` and appends `[image saved: <path>]` instead.

Either way append the observation to the spec's *Surprises & Discoveries* and note the
Claude Code version actually run (`claude --version`).

Recorded run (T12): **PROMOTE on Claude Code 2.1.238** — the spec bullet says 2.1.236, which
is the version the design was written against; 2.1.238 is what shipped by the time the spike
ran. Wire evidence: base64 `data` of 25 196 chars decoding to an 18 897-byte PNG,
sha256 `f4deb1b01495f328098e8389cc652b7849c0cc2f876453d5c1c4da71fe927e46`, equal to
`cells/2-0.png` in all three runs (keyless CLI, headless, interactive).

## 5. Cleanup

Kill only the kernels these runs created (`~/.ptc/kernels/<key>/owner.json` pid), and only
the tmux session created above.

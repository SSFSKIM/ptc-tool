/**
 * ptc agent host — spike S9 (M1 of docs/doperpowers/specs/2026-10-20-agent-host-design.md).
 *
 * Loaded into one headless `claude` the driver starts through the SDK. At
 * `session.start` it posts `/hello` to the channel named by PTC_HOST_SOCKET,
 * then long-polls `/jobs` and runs what it is given without waiting for it:
 * `complete` through `$.model.complete`, `agent` as a lean subagent type it
 * registers and spawns, whose `turn.complete` is the job's result. Every job
 * ends in exactly one result POST (protocol 1, "The channel").
 *
 * Spike-only parts, which M3 does not carry: the `probe` job kind (Q7: the
 * fetch body cap and a held request near 30 s), the per-job `cancelVia`
 * strategy (Q5), timing fields on each result (`t0`, `t1`: Date.now() at the
 * job's start and settle), and the diagnostics on `/hello` (main-loop tools,
 * session id), which the driver reads as evidence.
 */
import type { EngineInterface, Register } from 'claude-code'

const PROTOCOL = 1
const PLUGIN_VERSION = '0.0.1'
const BASE = 'http://host'
/** The poll's hold, under `$.http.fetch`'s 30 s bound. */
const POLL_WAIT_MS = 25_000
/** After a poll that failed, the next waits this long. */
const POLL_RETRY_MS = 500
/** A result POST is tried this many times within RESULT_RETRY_WINDOW_MS. */
const RESULT_TRIES = 3
const RESULT_RETRY_GAP_MS = 3_000
/** A result envelope past this many characters moves its text to a file. */
const ENVELOPE_SPILL = 3_500_000
/** `$.fs.write`'s cap, in UTF-8 bytes. */
const FS_CAP_BYTES = 4 * 1024 * 1024
const LEAN_DESCRIPTION = 'ptc lean agent (spawned by the ptc kernel only)'

type CancelVia = 'taskstop' | 'step' | 'tooldeny' | 'none'

type LeanType = {
  prompt: string
  tools: string[]
  model: string
  effort: string | null
  permissionMode: string
  maxTurns: number
}

type CompleteJob = {
  id: string
  kind: 'complete'
  model: string
  system: string | null
  prompt: string | null
  promptFile: string | null
  systemFile: string | null
  maxTokens: number
  effort: string | null
  timeoutMs: number | null
}

type AgentJob = {
  id: string
  kind: 'agent'
  type: LeanType
  prompt: string | null
  promptFile: string | null
  cwd: string | null
  description: string
  timeoutMs: number | null
  cancelVia?: CancelVia
}

type ProbeJob = {
  id: string
  kind: 'probe'
  probe: 'post-size' | 'get-size' | 'hold'
  values: number[]
}

type Job = CompleteJob | AgentJob | ProbeJob

type Result = Record<string, unknown> & { ok: boolean }

type Running = {
  job: Job
  t0: number
  controller?: AbortController
  agentId?: string
  cancelRequested?: boolean
  settled?: boolean
}

const state = {
  socketPath: undefined as string | undefined,
  outDir: undefined as string | undefined,
  stopped: false,
  started: false,
  running: new Map<string, Running>(),
  byAgent: new Map<string, string>(),
  /** turn.complete events that beat their spawn's answer to the poll loop. */
  early: new Map<string, { reason: string; answer: string; usage: unknown }>(),
  types: new Map<string, Promise<string>>(),
  /** Agents whose cancel strategy acts in a hook (`step`, `tooldeny`). */
  stepCancelled: new Set<string>(),
  toolCancelled: new Set<string>(),
}

function canonical(v: unknown): string {
  if (Array.isArray(v)) {
    return `[${v.map(canonical).join(',')}]`
  }
  if (v !== null && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`).join(',')}}`
  }
  return JSON.stringify(v)
}

async function sha1hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length
}

async function request($: EngineInterface, path: string, body?: unknown) {
  const socketPath = state.socketPath
  if (socketPath === undefined) {
    throw new Error('no channel')
  }
  return body === undefined
    ? $.http.fetch(`${BASE}${path}`, { socketPath })
    : $.http.fetch(`${BASE}${path}`, {
        method: 'POST',
        socketPath,
        headers: { 'content-type': 'application/json' },
        body: typeof body === 'string' ? body : JSON.stringify(body),
      })
}

/**
 * The job's one result POST. A text whose envelope would pass ENVELOPE_SPILL
 * characters goes to `<outDir>/<id>.txt`; one past the write cap is
 * `too-large`. Tried RESULT_TRIES times; when none lands the mod stops
 * polling, and the kernel's lease retires the generation.
 */
async function post($: EngineInterface, id: string, result: Result): Promise<void> {
  const run = state.running.get(id)
  if (run !== undefined) {
    if (run.settled) {
      return
    }
    run.settled = true
    result = { ...result, t0: run.t0, t1: Date.now() }
    if (run.agentId !== undefined) {
      state.byAgent.delete(run.agentId)
      state.stepCancelled.delete(run.agentId)
      state.toolCancelled.delete(run.agentId)
    }
    state.running.delete(id)
  }
  let body = JSON.stringify(result)
  if (body.length > ENVELOPE_SPILL && typeof result.text === 'string') {
    const text = result.text
    const bytes = utf8Bytes(text)
    if (bytes > FS_CAP_BYTES || state.outDir === undefined) {
      body = JSON.stringify({ ok: false, reason: 'too-large', detail: `${bytes} bytes of text`, t0: result.t0, t1: result.t1 })
    } else {
      const textFile = `${state.outDir}/${id}.txt`
      await $.fs.write(textFile, text)
      body = JSON.stringify({ ...result, text: null, textFile })
    }
  }
  for (let attempt = 1; attempt <= RESULT_TRIES; attempt++) {
    try {
      const res = await request($, `/results/${encodeURIComponent(id)}`, body)
      if (res.status === 200 || res.status === 404) {
        return
      }
    } catch {
      // a transport failure: tried again below
    }
    if (attempt < RESULT_TRIES) {
      await $.clock.sleep(RESULT_RETRY_GAP_MS)
    }
  }
  state.stopped = true
}

async function textOf($: EngineInterface, inline: string | null, file: string | null): Promise<string> {
  if (file !== null) {
    const read = await $.fs.read(file)
    return typeof read === 'string' ? read : ''
  }
  return inline ?? ''
}

async function runComplete($: EngineInterface, job: CompleteJob, run: Running): Promise<void> {
  const controller = new AbortController()
  run.controller = controller
  const prompt = await textOf($, job.prompt, job.promptFile)
  const system = job.systemFile !== null || job.system !== null ? await textOf($, job.system, job.systemFile) : undefined
  const r = await $.model.complete(
    {
      model: job.model,
      prompt,
      ...(system !== undefined ? { system } : {}),
      maxTokens: job.maxTokens,
      ...(job.effort !== null ? { effort: job.effort as 'low' | 'medium' | 'high' | 'xhigh' | 'max' } : {}),
      ...(job.timeoutMs !== null ? { timeoutMs: Math.max(1, job.timeoutMs) } : {}),
    },
    { signal: controller.signal },
  )
  if (r.isAnswered) {
    await post($, job.id, { ok: true, text: r.text, usage: r.usage })
  } else if (r.reason === 'api-error') {
    await post($, job.id, { ok: false, reason: 'api-error', detail: r.error, status: r.status, usage: r.usage })
  } else if (r.reason === 'aborted') {
    await post($, job.id, { ok: false, reason: run.cancelRequested ? 'aborted' : 'timeout', detail: 'aborted', usage: r.usage })
  } else {
    await post($, job.id, { ok: false, reason: 'error', detail: r.reason, usage: r.usage })
  }
}

/** The lean type for this job's `type`, registered once: `lean-<sha1[:10]>`. */
function leanType($: EngineInterface, type: LeanType): Promise<string> {
  const key = canonical(type)
  let registered = state.types.get(key)
  if (registered === undefined) {
    registered = (async () => {
      const name = `lean-${(await sha1hex(key)).slice(0, 10)}`
      const { agent } = await $.agent.register({
        name,
        description: LEAN_DESCRIPTION,
        prompt: type.prompt,
        tools: type.tools,
        model: type.model,
        ...(type.effort !== null ? { effort: type.effort } : {}),
        permissionMode: type.permissionMode,
        maxTurns: type.maxTurns,
        omitClaudeMd: true,
      })
      return agent
    })()
    state.types.set(key, registered)
    registered.catch(() => state.types.delete(key))
  }
  return registered
}

async function runAgent($: EngineInterface, job: AgentJob, run: Running): Promise<void> {
  const subagentType = await leanType($, job.type)
  const prompt = await textOf($, job.prompt, job.promptFile)
  const spawned = await $.agent.spawn({
    subagentType,
    prompt,
    description: job.description,
    model: job.type.model,
    ...(job.cwd !== null ? { cwd: job.cwd } : {}),
  })
  if (spawned.deny !== undefined) {
    await post($, job.id, { ok: false, reason: 'denied', detail: spawned.deny })
    return
  }
  const agentId = spawned.agentId
  if (agentId === undefined) {
    await post($, job.id, { ok: false, reason: 'error', detail: 'spawn answered no agentId' })
    return
  }
  run.agentId = agentId
  state.byAgent.set(agentId, job.id)
  const early = state.early.get(agentId)
  if (early !== undefined) {
    state.early.delete(agentId)
    await settleAgent($, job.id, agentId, early)
    return
  }
  if (run.cancelRequested) {
    await cancelAgent($, job, run)
  }
}

async function settleAgent($: EngineInterface, jobId: string, agentId: string, e: { reason: string; answer: string; usage: unknown }) {
  if (e.reason === 'answer') {
    await post($, jobId, { ok: true, text: e.answer, usage: e.usage ?? null, agentId })
  } else {
    await post($, jobId, { ok: false, reason: e.reason, detail: e.answer, usage: e.usage ?? null, agentId })
  }
}

/** Q5: the strategy the job names; the job's one result still comes from its turn.complete. */
async function cancelAgent($: EngineInterface, job: AgentJob, run: Running): Promise<void> {
  const agentId = run.agentId
  if (agentId === undefined) {
    return // applied once the spawn answers
  }
  const via = job.cancelVia ?? 'taskstop'
  if (via === 'taskstop') {
    try {
      await $.tool.call({ tool: 'TaskStop', task_id: agentId })
    } catch (err) {
      $.ui.log(`ptc-host: TaskStop ${agentId} failed: ${String(err)}`, { to: 'debug' })
    }
  } else if (via === 'step') {
    state.stepCancelled.add(agentId)
  } else if (via === 'tooldeny') {
    state.toolCancelled.add(agentId)
  }
}

async function runProbe($: EngineInterface, job: ProbeJob): Promise<void> {
  const report: Record<string, unknown>[] = []
  for (const n of job.values) {
    const t = Date.now()
    try {
      if (job.probe === 'post-size') {
        const res = await request($, `/probe/echo?n=${n}`, 'x'.repeat(n))
        report.push({ n, status: res.status, reply: res.text.slice(0, 200), ms: Date.now() - t })
      } else if (job.probe === 'get-size') {
        const res = await request($, `/probe/big?n=${n}`)
        report.push({ n, status: res.status, length: res.text.length, ms: Date.now() - t })
      } else {
        const res = await request($, `/probe/hold?ms=${n}`)
        report.push({ n, status: res.status, ms: Date.now() - t })
      }
    } catch (err) {
      report.push({ n, error: String(err), ms: Date.now() - t })
    }
  }
  await post($, job.id, { ok: true, text: JSON.stringify(report) })
}

async function dispatch($: EngineInterface, job: Job): Promise<void> {
  const run: Running = { job, t0: Date.now() }
  state.running.set(job.id, run)
  try {
    if (job.kind === 'complete') {
      await runComplete($, job, run)
    } else if (job.kind === 'agent') {
      await runAgent($, job, run)
    } else {
      await runProbe($, job)
    }
  } catch (err) {
    // A mod failure surfaces as a failed call, never as a hang.
    await post($, job.id, { ok: false, reason: 'error', detail: String(err) })
  }
}

function cancel($: EngineInterface, id: string): void {
  const run = state.running.get(id)
  if (run === undefined || run.settled) {
    return
  }
  run.cancelRequested = true
  if (run.job.kind === 'complete') {
    run.controller?.abort()
  } else if (run.job.kind === 'agent') {
    void cancelAgent($, run.job, run)
  }
}

async function pollLoop($: EngineInterface): Promise<void> {
  let ack = 0
  while (!state.stopped) {
    let res
    try {
      res = await request($, `/jobs?wait=${POLL_WAIT_MS}&ack=${ack}`)
    } catch {
      await $.clock.sleep(POLL_RETRY_MS)
      continue
    }
    if (res.status !== 200) {
      await $.clock.sleep(POLL_RETRY_MS)
      continue
    }
    let body: { seq?: number; jobs?: Job[]; cancel?: string[]; stop?: boolean }
    try {
      body = JSON.parse(res.text)
    } catch {
      continue
    }
    if (typeof body.seq === 'number' && body.seq > ack) {
      ack = body.seq
    }
    for (const id of body.cancel ?? []) {
      cancel($, id)
    }
    for (const job of body.jobs ?? []) {
      void dispatch($, job)
    }
    if (body.stop === true) {
      state.stopped = true
    }
  }
}

async function start($: EngineInterface, cwd: string): Promise<void> {
  const socketPath = await $.env.get('PTC_HOST_SOCKET')
  if (socketPath === undefined || socketPath === '') {
    return // not a host: loaded anywhere else, the mod does nothing
  }
  state.socketPath = socketPath
  const [version, sessionId, tools] = await Promise.all([
    $.session.version(),
    $.session.id(),
    $.tool.list().then((list) => list.map((t) => t.name), (err) => [`error: ${String(err)}`]),
  ])
  const res = await request($, '/hello', {
    protocol: PROTOCOL,
    engine: version.version,
    plugin: PLUGIN_VERSION,
    spike: { sessionId, cwd, mainTools: tools, at: Date.now() },
  })
  if (res.status !== 200) {
    return // protocol mismatch: the kernel fails the start
  }
  try {
    const reply = JSON.parse(res.text) as { outDir?: string }
    state.outDir = reply.outDir
  } catch {
    // no outDir: a large result is answered too-large
  }
  void pollLoop($)
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    if (!state.started) {
      state.started = true
      void start($, e.cwd)
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const agentId = e.agentId
    if (agentId !== undefined) {
      const jobId = state.byAgent.get(agentId)
      const settled = { reason: e.reason, answer: e.answer, usage: e.usage }
      if (jobId !== undefined) {
        void settleAgent($, jobId, agentId, settled)
      } else if (state.socketPath !== undefined) {
        state.early.set(agentId, settled)
      }
    }
    return next(e)
  })

  // Lean types are the kernel's to spawn; the model is never offered them.
  on('agent.offer', { agent: /^ptc-host:lean-/ }, () => ({ isOffered: false }))

  // Q5, `step`: a cancelled agent's next request is answered here, with no tool
  // use, so its loop ends on that turn.
  on('turn.step', async function* ($, e, next) {
    if (e.agentId === undefined || !state.stepCancelled.has(e.agentId)) {
      return yield* next(e)
    }
    yield { kind: 'text', index: 0, text: 'cancelled by ptc' }
    yield { kind: 'stop', stopReason: 'end_turn', usage: null }
    return { turnId: e.turnId, index: e.index, answer: 'cancelled by ptc', toolUses: [], stopReason: 'end_turn', usage: null }
  })

  // Q5, `tooldeny`: every tool call of a cancelled agent is refused.
  on('tool.call', async ($, e, next) => {
    if (e.agentId !== undefined && state.toolCancelled.has(e.agentId)) {
      return { deny: 'cancelled by ptc' }
    }
    return next(e)
  })
}

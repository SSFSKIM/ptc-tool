/**
 * ptc's agent host (docs/doperpowers/specs/2026-10-20-agent-host-design.md).
 *
 * Loaded into the one headless `claude` a ptc kernel starts (`src/ptc/runtime/host.py`),
 * and only there: anywhere PTC_HOST_SOCKET is unset it does nothing. At `session.start`
 * it posts `/hello` to the kernel's channel, then long-polls `/jobs` and starts every job
 * without waiting for it: a `complete` through `$.model.complete`, an `agent` as a lean
 * subagent type it registers once and spawns, whose `turn.complete` is the job's result.
 * Every job ends in exactly one result POST (protocol 1, "The channel"); the kernel
 * releases the job's slot when that result arrives, even after its caller gave up.
 *
 * The host's main loop never runs a model turn. A spawn goes through its `Agent` tool and
 * a cancel through its `TaskStop`, so the kernel starts it with the default tool set.
 */
import type { EngineInterface, Register } from 'claude-code'

const PROTOCOL = 1
const PLUGIN_VERSION = '1.0.0'
const BASE = 'http://host'
/** The poll's hold, under `$.http.fetch`'s 30 s bound (M1, Q7). */
const POLL_WAIT_MS = 25_000
/** After a failed poll the next waits this long. */
const POLL_RETRY_MS = 500
/** Polls failing this long in a row mean the kernel is gone (its lease has lapsed long
 *  before): the loop stops rather than spin. */
const POLL_GIVE_UP_MS = 120_000
/** A result POST is tried this many times, all within RESULT_WINDOW_MS; when none lands
 *  the mod stops polling, and the kernel's lease retires this host. */
const RESULT_TRIES = 3
const RESULT_WINDOW_MS = 10_000
const RESULT_GAP_MS = 2_000
/** A result envelope past this many characters moves its text to a file. */
const ENVELOPE_SPILL = 3_500_000
/** `$.fs.write`'s cap, in UTF-8 bytes. */
const FS_CAP_BYTES = 4 * 1024 * 1024
const LEAN_DESCRIPTION = 'ptc lean agent (spawned by the ptc kernel only)'

type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'

type LeanType = {
  prompt: string
  tools: string[]
  model: string | null
  effort: string | null
  permissionMode: string
  maxTurns: number
}

type CompleteJob = {
  id: string
  kind: 'complete'
  model: string
  system: string | null
  systemFile: string | null
  prompt: string | null
  promptFile: string | null
  /** null: the most the model gives in one completion (`HOST_MAX_TOKENS`, or less) */
  maxTokens: number | null
  effort: string | null
  timeoutMs: number | null
}

type AgentJob = {
  id: string
  kind: 'agent'
  type: LeanType
  prompt: string | null
  promptFile: string | null
  description: string
  timeoutMs: number | null
}

type Job = CompleteJob | AgentJob

type Result = Record<string, unknown> & { ok: boolean }

type Ended = { reason: string; answer: string; usage: unknown }

type Running = {
  job: Job
  controller?: AbortController
  agentId?: string
  cancelRequested?: boolean
  settled?: boolean
}

const state = {
  socketPath: undefined as string | undefined,
  outDir: undefined as string | undefined,
  started: false,
  stopped: false,
  running: new Map<string, Running>(),
  byAgent: new Map<string, string>(),
  /** turn.complete events that beat their spawn's answer back to the dispatch, kept only
   *  while a spawn is in flight (nothing else could claim one). */
  early: new Map<string, Ended>(),
  spawning: 0,
  types: new Map<string, Promise<string>>(),
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

async function request($: EngineInterface, path: string, body?: string) {
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
        body,
      })
}

/** `p`, or undefined once `ms` have passed: `$.http.fetch` takes no signal of its own. */
async function within<T>($: EngineInterface, ms: number, p: Promise<T>): Promise<T | undefined> {
  const stop = new AbortController()
  const timer = $.clock.sleep(ms, { signal: stop.signal }).then(
    () => undefined,
    () => undefined,
  )
  try {
    return await Promise.race([p, timer])
  } finally {
    stop.abort()
  }
}

/** The envelope as posted: a text past ENVELOPE_SPILL characters of JSON goes to
 *  `<outDir>/<id>.txt`, and one past the write cap becomes `too-large`. */
async function envelope($: EngineInterface, id: string, result: Result): Promise<string> {
  const body = JSON.stringify(result)
  if (body.length <= ENVELOPE_SPILL || typeof result.text !== 'string') {
    return body
  }
  const bytes = new TextEncoder().encode(result.text).length
  if (bytes > FS_CAP_BYTES || state.outDir === undefined) {
    return JSON.stringify({ ok: false, reason: 'too-large', detail: `the reply is ${bytes} bytes of text, over the 4 MiB a file write takes` })
  }
  const textFile = `${state.outDir}/${id}.txt`
  await $.fs.write(textFile, result.text)
  return JSON.stringify({ ...result, text: null, textFile })
}

/** The job's one result POST. Tried RESULT_TRIES times within RESULT_WINDOW_MS; when no
 *  try lands, the mod stops polling and the kernel's lease retires the host, so a lost
 *  result ends in a bounded retirement and never in a stranded call. */
async function post($: EngineInterface, id: string, result: Result): Promise<void> {
  const run = state.running.get(id)
  if (run?.settled) {
    return
  }
  if (run !== undefined) {
    run.settled = true
    if (run.agentId !== undefined) {
      state.byAgent.delete(run.agentId)
    }
    state.running.delete(id)
  }
  let body: string
  try {
    body = await envelope($, id, result)
  } catch (err) {
    body = JSON.stringify({ ok: false, reason: 'error', detail: `the result could not be written: ${String(err)}` })
  }
  const until = Date.now() + RESULT_WINDOW_MS
  for (let attempt = 1; attempt <= RESULT_TRIES; attempt++) {
    const left = until - Date.now()
    if (left <= 0) {
      break
    }
    const status = await within(
      $,
      left,
      request($, `/results/${encodeURIComponent(id)}`, body).then(
        (res) => res.status,
        () => -1,
      ),
    )
    if (status === 200 || status === 404) {
      return
    }
    const gap = Math.min(RESULT_GAP_MS, until - Date.now())
    if (attempt < RESULT_TRIES && gap > 0) {
      await $.clock.sleep(gap)
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

/** `$.model.complete`'s own ceiling on a reply, whatever the model can give. */
const HOST_MAX_TOKENS = 64000
const PAST_WHAT_IT_CAN = /is past what .* can produce in one reply \((\d+)\)/

async function runComplete($: EngineInterface, job: CompleteJob, run: Running): Promise<void> {
  const controller = new AbortController()
  run.controller = controller
  if (run.cancelRequested) {
    controller.abort()
  }
  const prompt = await textOf($, job.prompt, job.promptFile)
  const system = await textOf($, job.system, job.systemFile)
  const call = (maxTokens: number) =>
    $.model.complete(
      {
        model: job.model,
        prompt,
        ...(system !== '' ? { system } : {}),
        maxTokens,
        ...(job.effort !== null ? { effort: job.effort as Effort } : {}),
        ...(job.timeoutMs !== null ? { timeoutMs: Math.max(1, job.timeoutMs) } : {}),
      },
      { signal: controller.signal },
    )
  // A cap of null is the most the model gives: the engine's ceiling, or, for a model that
  // gives less, the figure the engine names when it refuses that ceiling.
  let limit = job.maxTokens ?? HOST_MAX_TOKENS
  let r
  try {
    r = await call(limit)
  } catch (e) {
    const less = job.maxTokens === null ? PAST_WHAT_IT_CAN.exec(String(e)) : null
    if (less === null) throw e
    limit = Number(less[1])
    r = await call(limit)
  }
  // The engine reports no stop reason, and asks the API for more than `maxTokens` (a
  // thinking model's headroom). A reply that spent the whole cap is the one the CLI route
  // cuts there and fails, in these words; it fails here too, its text riding along.
  if (r.isAnswered && r.usage.output_tokens >= limit) {
    await post($, job.id, {
      ok: false,
      reason: 'max-tokens',
      detail: `API Error: Claude's response exceeded the ${limit} output token maximum.`,
      text: r.text,
      usage: r.usage,
    })
  } else if (r.isAnswered) {
    await post($, job.id, { ok: true, text: r.text, usage: r.usage })
  } else if (r.reason === 'api-error') {
    await post($, job.id, { ok: false, reason: 'api-error', detail: r.error, status: r.status, usage: r.usage })
  } else if (r.reason === 'aborted') {
    // Our own cancel, or the call's own time limit.
    const reason = run.cancelRequested ? 'aborted' : 'timeout'
    await post($, job.id, { ok: false, reason, detail: 'the model call was cut', usage: r.usage })
  } else {
    await post($, job.id, { ok: false, reason: 'error', detail: r.reason, usage: r.usage })
  }
}

/** The job's lean type, registered once: `lean-<first 10 hex of sha1(canonical JSON)>`. */
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
        ...(type.model !== null ? { model: type.model } : {}),
        ...(type.effort !== null ? { effort: type.effort } : {}),
        permissionMode: type.permissionMode,
        maxTurns: type.maxTurns,
        omitClaudeMd: true,
      })
      return agent
    })()
    state.types.set(key, registered)
    // A failed registration is not cached: the next job of this type tries again.
    registered.catch(() => state.types.delete(key))
  }
  return registered
}

async function settleAgent($: EngineInterface, jobId: string, agentId: string, e: Ended): Promise<void> {
  if (e.reason === 'answer') {
    await post($, jobId, { ok: true, text: e.answer, usage: e.usage ?? null, agentId })
  } else {
    await post($, jobId, { ok: false, reason: e.reason, detail: e.answer, usage: e.usage ?? null, agentId })
  }
}

async function stopAgent($: EngineInterface, agentId: string): Promise<void> {
  // M1, Q5: TaskStop ends a running subagent, whose turn.complete then reads `aborted`.
  try {
    await $.tool.call({ tool: 'TaskStop', task_id: agentId })
  } catch (err) {
    $.ui.log(`ptc-host: TaskStop ${agentId} failed: ${String(err)}`, { to: 'debug' })
  }
}

async function runAgent($: EngineInterface, job: AgentJob, run: Running): Promise<void> {
  if (run.cancelRequested) {
    await post($, job.id, { ok: false, reason: 'aborted', detail: 'cancelled before it started' })
    return
  }
  const subagentType = await leanType($, job.type)
  const prompt = await textOf($, job.prompt, job.promptFile)
  state.spawning++
  let spawned: Awaited<ReturnType<typeof $.agent.spawn>>
  try {
    spawned = await $.agent.spawn({
      subagentType,
      prompt,
      description: job.description,
      ...(job.type.model !== null ? { model: job.type.model } : {}),
    })
  } finally {
    state.spawning--
  }
  if (spawned.deny !== undefined) {
    await post($, job.id, { ok: false, reason: 'denied', detail: spawned.deny })
    return
  }
  const agentId = spawned.agentId
  if (agentId === undefined) {
    await post($, job.id, { ok: false, reason: 'error', detail: 'the spawn answered no agentId' })
    return
  }
  run.agentId = agentId
  state.byAgent.set(agentId, job.id)
  const early = state.early.get(agentId)
  if (early !== undefined) {
    state.early.delete(agentId)
    await settleAgent($, job.id, agentId, early)
  } else if (run.cancelRequested) {
    await stopAgent($, agentId)
  }
}

async function dispatch($: EngineInterface, job: Job): Promise<void> {
  const run: Running = { job }
  state.running.set(job.id, run)
  try {
    if (job.kind === 'complete') {
      await runComplete($, job, run)
    } else if (job.kind === 'agent') {
      await runAgent($, job, run)
    } else {
      const unknown = job as { id: string; kind: unknown }
      await post($, unknown.id, { ok: false, reason: 'error', detail: `no job kind ${String(unknown.kind)}` })
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
  } else if (run.agentId !== undefined) {
    void stopAgent($, run.agentId)
  }
  // An agent still spawning is stopped once its spawn answers (runAgent).
}

async function pollLoop($: EngineInterface): Promise<void> {
  let ack = 0
  let failingSince: number | undefined
  while (!state.stopped) {
    let res
    try {
      res = await request($, `/jobs?wait=${POLL_WAIT_MS}&ack=${ack}`)
    } catch {
      res = undefined
    }
    if (res === undefined || res.status !== 200) {
      if (res?.status === 410) {
        return // retired: the kernel is done with this host
      }
      failingSince ??= Date.now()
      if (Date.now() - failingSince > POLL_GIVE_UP_MS) {
        return
      }
      await $.clock.sleep(POLL_RETRY_MS)
      continue
    }
    failingSince = undefined
    let body: { seq?: number; jobs?: Job[]; cancel?: string[] }
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
  }
}

async function start($: EngineInterface, cwd: string): Promise<void> {
  const socketPath = await $.env.get('PTC_HOST_SOCKET')
  if (socketPath === undefined || socketPath === '') {
    return // not a host: loaded anywhere else, the mod does nothing
  }
  state.socketPath = socketPath
  const [version, session] = await Promise.all([$.session.version(), $.session.id()])
  const res = await request(
    $,
    '/hello',
    JSON.stringify({ protocol: PROTOCOL, engine: version.version, plugin: PLUGIN_VERSION, session, cwd }),
  )
  if (res.status !== 200) {
    return // a protocol mismatch: the kernel fails the start
  }
  try {
    state.outDir = (JSON.parse(res.text) as { outDir?: string }).outDir
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
      void start($, e.cwd).catch((err) => $.ui.log(`ptc-host: start failed: ${String(err)}`, { to: 'debug' }))
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const agentId = e.agentId
    if (agentId !== undefined && state.socketPath !== undefined) {
      const ended = { reason: e.reason, answer: e.answer, usage: e.usage }
      const jobId = state.byAgent.get(agentId)
      if (jobId !== undefined) {
        void settleAgent($, jobId, agentId, ended)
      } else if (state.spawning > 0) {
        state.early.set(agentId, ended)
      }
    }
    return next(e)
  })

  // Lean types are the kernel's to spawn; the model is never offered them.
  on('agent.offer', { agent: /^ptc-host:lean-/ }, () => ({ isOffered: false }))
}

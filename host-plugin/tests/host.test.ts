/**
 * The agent host's mod (`hooks/mods/host.tsx`) against a fake kernel and a fake engine.
 *
 * Run by `claude plugin test host-plugin` (from `test/unit/test_host_mod.py`): the kit
 * loads this plugin as the host loads it, and every hook a test registers on `on` sits
 * beneath it, so the test's hooks are the world the mod talks to — the kernel's channel
 * (`http.fetch` on the socket), the model (`model.complete`), the subagents
 * (`agent.register`, `agent.spawn`, `TaskStop` through `tool.call`), the disk (`fs.*`) and
 * the clock (`mock.clock`), which moves only when a test moves it. Each test gets a fresh
 * instance of the module, so its module-level state starts empty every time.
 *
 * Protocol 1 is "The channel" in docs/doperpowers/specs/2026-10-20-agent-host-design.md.
 */
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { describe, expect, mock, test } from 'claude-code/testing'

const SOCKET = '/k/host/g1.sock'
const OUT_DIR = '/k/host/out'
const USAGE = { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
const TYPE = { prompt: 'You read files.', tools: ['Read'], model: 'haiku', effort: null, permissionMode: 'bypassPermissions', maxTurns: 50 }

type Json = Record<string, unknown>
type Reply = { status: number; text?: string }
type Answer = Reply | Promise<Reply> | 'throw'

/** A promise the test settles by hand: a call held in flight until the test lets it go. */
function deferred<T = void>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => (resolve = r))
  return { promise, resolve }
}

const completeJob = (id: string, extra: Json = {}) => ({
  id,
  kind: 'complete',
  model: 'haiku',
  system: null,
  systemFile: null,
  prompt: 'hi',
  promptFile: null,
  maxTokens: 4096,
  effort: null,
  timeoutMs: null,
  ...extra,
})

const agentJob = (id: string, extra: Json = {}) => ({
  id,
  kind: 'agent',
  type: TYPE,
  prompt: 'read it',
  promptFile: null,
  description: 'ptc agent',
  timeoutMs: null,
  ...extra,
})

/** A posted body with a long text cut to its length: a failing check on a multi-megabyte
 *  text would print all of it. */
const brief = (body: Json) => (typeof body.text === 'string' && body.text.length > 200 ? { ...body, text: `<${body.text.length} chars>` } : body)

const answered = (text: string, usage: Json = USAGE) => ({ value: { isAnswered: true, text, usage } })

/** A spawn that started agent `agentId`, as core answers it. */
const started = (agentId: string) => ({ agentId, model: 'haiku', result: { agentId, resolvedModel: 'haiku' } })

/** A model call that runs until its dispatch is aborted, then resolves as core does. */
const running = (next: { signal: AbortSignal }) =>
  new Promise((resolve) => {
    next.signal.addEventListener('abort', () =>
      resolve({ value: { isAnswered: false, reason: 'aborted', usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } }),
    )
  })

/**
 * The kernel's side of the channel and the engine beneath the mod. `opts` swaps one piece
 * of the world; everything else answers as a healthy kernel and engine do.
 */
function world($: Engine, on: On, opts: {
  env?: Record<string, string>
  hello?: Reply
  result?: (id: string, body: Json, n: number) => Answer
  complete?: (e: Json, n: number, next: { signal: AbortSignal }) => unknown
  register?: (e: Json, n: number) => unknown
  spawn?: (e: Json, n: number) => unknown
  /** The disk: a file's text, or `{ deny }` for one the engine cannot read; a path not
   *  listed is missing. */
  files?: Record<string, string | Promise<string> | { deny: string }>
  write?: (path: string, text: string) => unknown
} = {}) {
  mock.env(on, opts.env ?? { PTC_HOST_SOCKET: SOCKET })
  const clock = mock.clock(on, { now: 1_000_000 })
  const fetches: { url: string; init?: Json }[] = []
  const polls: { ack: number; wait: number }[] = []
  const results: { id: string; body: Json }[] = []
  const completes: Json[] = []
  const registers: Json[] = []
  const spawns: Json[] = []
  const stops: string[] = []
  const reads: string[] = []
  const writes: { path: string; text: string }[] = []
  // The poll the mod holds open, and responses queued before it arrived.
  let held: ((r: Reply) => void) | undefined
  const queued: Reply[] = []
  // While set, every poll fails at once, as a kernel that is gone.
  let down: Reply | undefined

  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.version', () => ({ value: { version: '2.1.306' } }))
  on('session.id', () => ({ value: 'host-session' }))
  on('ui.log', () => ({ value: undefined }))
  on('http.fetch', async (_, e) => {
    fetches.push({ url: e.url, init: e.init as Json | undefined })
    expect(e.init?.socketPath).toBe(SOCKET)
    const url = new URL(e.url)
    let reply: Reply
    if (url.pathname === '/hello') {
      reply = opts.hello ?? { status: 200, text: JSON.stringify({ ok: true, outDir: OUT_DIR }) }
    } else if (url.pathname === '/jobs') {
      polls.push({ ack: Number(url.searchParams.get('ack')), wait: Number(url.searchParams.get('wait')) })
      reply = down ?? queued.shift() ?? (await new Promise<Reply>((r) => (held = r)))
    } else if (url.pathname.startsWith('/results/')) {
      const id = decodeURIComponent(url.pathname.slice('/results/'.length))
      const body = JSON.parse(String(e.init?.body)) as Json
      results.push({ id, body })
      const answer = opts.result?.(id, body, results.filter((r) => r.id === id).length) ?? { status: 200, text: '{"ok":true}' }
      if (answer === 'throw') {
        return { deny: 'connect ECONNREFUSED' }
      }
      reply = await answer
    } else {
      throw new Error(`no route ${e.url}`)
    }
    return { value: { status: reply.status, ok: reply.status < 300, headers: {}, text: reply.text ?? '' } }
  })
  on('model.complete', (_, e, next) => {
    completes.push(e as Json)
    return (opts.complete?.(e as Json, completes.length, next) ?? answered('pong')) as never
  })
  on('agent.register', (_, e) => {
    registers.push(e as Json)
    return (opts.register?.(e as Json, registers.length) ?? { value: { agent: `ptc-host:${e.name}` } }) as never
  })
  // A plugin's `$.agent.spawn` reaches this chain as the Agent tool's call (`subagent_type`,
  // `run_in_background`) and reads the started agent's id off the answer's `result`, the
  // tool's output (2.1.306). The fake reads either spelling and answers in both.
  on('agent.spawn', (_, e) => {
    const input = e as Json
    spawns.push({ subagentType: input.subagentType ?? input.subagent_type, prompt: input.prompt, description: input.description, model: input.model })
    const answer = opts.spawn?.(input, spawns.length) ?? started(`a${spawns.length}`)
    return answer as never
  })
  on('tool.call', (_, e) => {
    const input = e as Json
    expect(input.tool).toBe('TaskStop')
    stops.push(String(input.task_id))
    return { result: { message: 'stopped' } as never }
  })
  on('fs.read', async (_, e) => {
    reads.push(e.path)
    const file = opts.files?.[e.path]
    if (file === undefined) {
      return { deny: `$.fs.read(${e.path}) failed: ENOENT` } // as the engine words it on 2.1.306
    }
    return typeof file === 'object' && 'deny' in file ? file : { value: await file }
  })
  on('fs.write', (_, e) => {
    writes.push({ path: e.path, text: e.text })
    return (opts.write?.(e.path, e.text) ?? { value: undefined }) as never
  })
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('agent.offer', () => ({ isOffered: true }))

  let seq = 0
  /** Answers the held poll, or the next one: a poll response as the kernel builds it. */
  const reply = async (r: Reply) => {
    if (held !== undefined) {
      const h = held
      held = undefined
      h(r)
    } else {
      queued.push(r)
    }
    await clock.settle()
  }
  const send = (body: { jobs?: Json[]; cancel?: string[]; seq?: number }) =>
    reply({ status: 200, text: JSON.stringify({ seq: body.seq ?? ++seq, jobs: body.jobs ?? [], cancel: body.cancel ?? [] }) })

  return {
    clock,
    fetches,
    polls,
    results,
    completes,
    registers,
    spawns,
    stops,
    reads,
    writes,
    reply,
    send,
    /** Every poll from now on fails with this, or (undefined) is held again. */
    setDown: (r: Reply | undefined) => {
      down = r
    },
    /** Whether a poll is held open now. */
    isPolling: () => held !== undefined,
    start: async () => {
      await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
      await clock.settle()
    },
    /** A subagent's turn ending, as the engine raises it to every plugin; `usage` null: the
     *  turn counted none (an interrupt). */
    end: async (agentId: string, reason: 'answer' | 'aborted' | 'error', answer: string, usage: Json | null = USAGE) => {
      await $.turn.complete({ agentId, reason, answer, durationMs: 1, isAborted: reason === 'aborted', turnId: `t-${agentId}`, ...(usage !== null ? { usage } : {}) } as never)
      await clock.settle()
    },
    /** The one result each job id was posted, by id. */
    resultOf: (id: string) => {
      const posted = results.filter((r) => r.id === id)
      expect(posted.length, `results posted for ${id}`).toBe(1)
      return posted[0]!.body
    },
  }
}

describe('start', () => {
  test('without PTC_HOST_SOCKET the mod does nothing', async ($, on) => {
    const w = world($, on, { env: {} })
    await w.start()
    await w.end('a1', 'answer', 'done')
    await w.clock.advance(60_000)
    expect(w.fetches).toEqual([])
  })

  test('hello names the protocol, the engine, the plugin, the session and its cwd; then the poll opens at ack 0', async ($, on) => {
    const w = world($, on)
    await w.start()
    expect(w.fetches[0]!.url).toBe('http://host/hello')
    expect(w.fetches[0]!.init?.method).toBe('POST')
    expect(JSON.parse(String(w.fetches[0]!.init?.body))).toEqual({ protocol: 1, engine: '2.1.306', plugin: '1.0.0', session: 'host-session', cwd: '/w' })
    expect(w.polls).toEqual([{ ack: 0, wait: 25_000 }])
  })

  test('a hello the kernel refuses (a protocol mismatch) opens no poll', async ($, on) => {
    const w = world($, on, { hello: { status: 409, text: '{"ok":false,"reason":"protocol"}' } })
    await w.start()
    await w.clock.advance(60_000)
    expect(w.fetches.map((f) => f.url)).toEqual(['http://host/hello'])
  })
})

describe('the poll', () => {
  test('each poll acks the seq of the last response received; a response with no seq leaves it', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ seq: 3 })
    await w.send({ seq: 4 })
    await w.reply({ status: 200, text: JSON.stringify({ jobs: [], cancel: [] }) })
    await w.reply({ status: 200, text: 'not json' })
    expect(w.polls.map((p) => p.ack)).toEqual([0, 3, 4, 4, 4])
  })

  test('410 ends the loop: the host is retired', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.reply({ status: 410 })
    await w.clock.advance(60_000)
    expect(w.polls).toHaveLength(1)
    expect(w.isPolling()).toBe(false)
  })

  test('a failed poll is tried again after 500 ms, keeping its ack', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ seq: 1 })
    await w.reply({ status: 503 })
    expect(w.polls).toHaveLength(2)
    await w.clock.advance(499)
    expect(w.polls).toHaveLength(2)
    await w.clock.advance(1)
    expect(w.polls.map((p) => p.ack)).toEqual([0, 1, 1])
  })

  test('polls failing for two minutes in a row end the loop; a success in between starts the count over', async ($, on) => {
    const w = world($, on)
    await w.start()
    w.setDown({ status: 503 })
    await w.reply({ status: 503 })
    await w.clock.advance(100_000)
    w.setDown(undefined)
    await w.clock.advance(500)
    expect(w.isPolling()).toBe(true)
    await w.send({})
    w.setDown({ status: 503 })
    await w.reply({ status: 503 })
    await w.clock.advance(100_000)
    const polled = w.polls.length
    await w.clock.advance(20_500)
    expect(w.polls.length).toBeGreaterThan(polled)
    const last = w.polls.length
    await w.clock.advance(10_000)
    expect(w.polls).toHaveLength(last)
  })
})

describe('complete jobs', () => {
  test('a job runs through $.model.complete with its fields; its reply is posted ok', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [completeJob('j1', { system: 'be brief', effort: 'low', timeoutMs: 30_000 })] })
    expect(w.completes).toEqual([{ model: 'haiku', prompt: 'hi', system: 'be brief', maxTokens: 4096, effort: 'low', timeoutMs: 30_000 }])
    expect(w.resultOf('j1')).toEqual({ ok: true, text: 'pong', usage: USAGE })
  })

  test('a job whose prompt and system were spilled reads them from their files', async ($, on) => {
    const w = world($, on, { files: { '/k/in/j1.prompt.txt': 'big prompt', '/k/in/j1.system.txt': 'big system' } })
    await w.start()
    await w.send({ jobs: [completeJob('j1', { prompt: null, promptFile: '/k/in/j1.prompt.txt', systemFile: '/k/in/j1.system.txt' })] })
    expect(w.completes[0]).toMatchObject({ prompt: 'big prompt', system: 'big system' })
    expect(w.resultOf('j1')).toMatchObject({ ok: true })
  })

  test('a reply that spent the whole cap fails as max-tokens, its text riding along', async ($, on) => {
    const w = world($, on, { complete: () => answered('cut sho', { ...USAGE, output_tokens: 4096 }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.resultOf('j1')).toEqual({
      ok: false,
      reason: 'max-tokens',
      detail: "API Error: Claude's response exceeded the 4096 output token maximum.",
      text: 'cut sho',
      usage: { ...USAGE, output_tokens: 4096 },
    })
  })

  test('no cap asks for the engine ceiling, then for the limit the engine names when the model gives less', async ($, on) => {
    const w = world($, on, {
      complete: (e) =>
        e.maxTokens === 64000 ? { deny: 'maxTokens 64000 is past what claude-haiku-4-5 can produce in one reply (8192)' } : answered('fits', { ...USAGE, output_tokens: 8192 }),
    })
    await w.start()
    await w.send({ jobs: [completeJob('j1', { maxTokens: null })] })
    expect(w.completes.map((c) => c.maxTokens)).toEqual([64000, 8192])
    // The cut is measured against the limit it ran with.
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'max-tokens', detail: expect.stringContaining('8192') })
  })

  test('an explicit cap the engine refuses is not retried', async ($, on) => {
    const w = world($, on, { complete: () => ({ deny: 'maxTokens 64000 is past what claude-haiku-4-5 can produce in one reply (8192)' }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1', { maxTokens: 64000 })] })
    expect(w.completes).toHaveLength(1)
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'error', detail: expect.stringContaining('past what') })
  })

  test('an api error is posted with its kind and status', async ($, on) => {
    const w = world($, on, { complete: () => ({ value: { isAnswered: false, reason: 'api-error', status: 529, error: 'overloaded', usage: USAGE } }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'api-error', detail: 'overloaded', status: 529, usage: USAGE })
  })

  test('a cancel cuts the running call and posts aborted', async ($, on) => {
    const w = world($, on, { complete: (_, __, next) => running(next) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.completes).toHaveLength(1)
    await w.send({ cancel: ['j1'] })
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'aborted' })
  })

  test('a cancel that lands while the prompt file is read makes no request and posts aborted', async ($, on) => {
    const file = deferred<string>()
    const w = world($, on, { files: { '/k/in/j1.prompt.txt': file.promise } })
    await w.start()
    await w.send({ jobs: [completeJob('j1', { prompt: null, promptFile: '/k/in/j1.prompt.txt' })] })
    await w.send({ cancel: ['j1'] })
    file.resolve('big prompt')
    await w.clock.settle()
    expect(w.completes).toEqual([])
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'aborted' })
  })

  test('a call cut by its own time limit, not by a cancel, posts timeout', async ($, on) => {
    const w = world($, on, { complete: () => ({ value: { isAnswered: false, reason: 'aborted', usage: USAGE } }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1', { timeoutMs: 5 })] })
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'timeout' })
  })
})

describe('agent jobs', () => {
  test("a job registers its lean type, spawns it, and posts its turn's answer", async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    expect(w.registers).toHaveLength(1)
    const name = String(w.registers[0]!.name)
    expect(name).toMatch(/^lean-[0-9a-f]{10}$/)
    expect(w.registers[0]).toMatchObject({
      description: 'ptc lean agent (spawned by the ptc kernel only)',
      prompt: TYPE.prompt,
      tools: TYPE.tools,
      model: 'haiku',
      permissionMode: 'bypassPermissions',
      maxTurns: 50,
      omitClaudeMd: true,
    })
    expect(w.registers[0]).not.toHaveProperty('effort')
    expect(w.spawns).toHaveLength(1)
    expect(w.spawns[0]).toMatchObject({ subagentType: `ptc-host:${name}`, prompt: 'read it', description: 'ptc agent', model: 'haiku' })
    expect(w.results).toEqual([])
    await w.end('a1', 'answer', 'the answer')
    expect(w.resultOf('j1')).toEqual({ ok: true, text: 'the answer', usage: USAGE, agentId: 'a1' })
  })

  test('an agent that ends on anything but an answer posts its reason, its partial text as text and detail, its id and usage', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    await w.end('a1', 'error', 'half of it')
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'error', detail: 'half of it', text: 'half of it', usage: USAGE, agentId: 'a1' })
  })

  test('a job posts once: a second end of its agent is not posted again', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    await w.end('a1', 'answer', 'first')
    await w.end('a1', 'answer', 'second')
    expect(w.resultOf('j1')).toMatchObject({ text: 'first' })
  })

  test("a turn.complete that beats its spawn's answer is the job's result", async ($, on) => {
    const spawn = deferred<unknown>()
    const w = world($, on, { spawn: () => spawn.promise })
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    await w.end('a1', 'answer', 'fast')
    expect(w.results).toEqual([])
    spawn.resolve(started('a1'))
    await w.clock.settle()
    expect(w.resultOf('j1')).toEqual({ ok: true, text: 'fast', usage: USAGE, agentId: 'a1' })
  })

  test("another agent's end, with no spawn in flight, is no job's", async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.end('stranger', 'answer', 'not ours')
    await w.send({ jobs: [agentJob('j1')] })
    await w.end('a1', 'answer', 'ours')
    expect(w.results.map((r) => r.id)).toEqual(['j1'])
    expect(w.resultOf('j1')).toMatchObject({ text: 'ours' })
  })

  test('one lean type per distinct type, whatever its key order; jobs of the same type reuse it', async ($, on) => {
    const w = world($, on)
    await w.start()
    const reordered = Object.fromEntries(Object.entries(TYPE).reverse())
    await w.send({ jobs: [agentJob('j1'), agentJob('j2', { type: reordered })] })
    await w.send({ jobs: [agentJob('j3'), agentJob('j4', { type: { ...TYPE, maxTurns: 5 } })] })
    expect(w.registers).toHaveLength(2)
    expect(w.registers[0]!.name).not.toBe(w.registers[1]!.name)
    expect(w.spawns.map((s) => s.subagentType)).toEqual([
      `ptc-host:${w.registers[0]!.name}`,
      `ptc-host:${w.registers[0]!.name}`,
      `ptc-host:${w.registers[0]!.name}`,
      `ptc-host:${w.registers[1]!.name}`,
    ])
  })

  test('a failed registration fails its job and is not kept: the next job of the type registers again', async ($, on) => {
    const w = world($, on, { register: (e, n) => (n === 1 ? { deny: 'invalid agent definition' } : { value: { agent: `ptc-host:${e.name}` } }) })
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'error', detail: expect.stringContaining('invalid agent definition') })
    await w.send({ jobs: [agentJob('j2')] })
    expect(w.registers).toHaveLength(2)
    expect(w.spawns).toHaveLength(1)
  })

  test('a refused spawn posts denied with its reason', async ($, on) => {
    const w = world($, on, { spawn: () => ({ deny: 'Agent is not allowed here' }) })
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'denied', detail: 'Agent is not allowed here' })
  })

  test('a cancel of a running agent stops it through TaskStop; its end is posted with the reason it ended on', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    await w.send({ cancel: ['j1'] })
    expect(w.stops).toEqual(['a1'])
    await w.end('a1', 'aborted', 'so far', null)
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'aborted', detail: 'so far', text: 'so far', usage: null, agentId: 'a1' })
  })

  test('a cancel while the spawn is in flight stops the agent once the spawn answers', async ($, on) => {
    const spawn = deferred<unknown>()
    const w = world($, on, { spawn: () => spawn.promise })
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    await w.send({ cancel: ['j1'] })
    expect(w.stops).toEqual([])
    spawn.resolve(started('a1'))
    await w.clock.settle()
    expect(w.stops).toEqual(['a1'])
  })

  test('a cancel that lands while the lean type registers spawns nothing and posts aborted', async ($, on) => {
    const registered = deferred<unknown>()
    const w = world($, on, { register: () => registered.promise })
    await w.start()
    await w.send({ jobs: [agentJob('j1')] })
    expect(w.registers).toHaveLength(1)
    await w.send({ cancel: ['j1'] })
    registered.resolve({ value: { agent: 'ptc-host:lean-0000000000' } })
    await w.clock.settle()
    expect(w.spawns).toEqual([])
    expect(w.stops).toEqual([])
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'aborted', detail: 'cancelled before it started' })
  })

  test('a cancel that lands while the prompt file is read spawns nothing and posts aborted', async ($, on) => {
    const file = deferred<string>()
    const w = world($, on, { files: { '/k/in/j1.prompt.txt': file.promise } })
    await w.start()
    await w.send({ jobs: [agentJob('j1', { prompt: null, promptFile: '/k/in/j1.prompt.txt' })] })
    expect(w.reads).toEqual(['/k/in/j1.prompt.txt'])
    await w.send({ cancel: ['j1'] })
    file.resolve('big prompt')
    await w.clock.settle()
    expect(w.spawns).toEqual([])
    expect(w.stops).toEqual([])
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'aborted', detail: 'cancelled before it started' })
  })

  test('lean types are never offered to the model; other agents are', async ($, on) => {
    world($, on)
    const offer = (agent: string) => $.agent.offer({ agent, description: 'x', source: 'plugin', provider: { plugin: 'ptc-host', tier: 'user' } } as never)
    expect(await offer('ptc-host:lean-0123456789')).toEqual({ isOffered: false })
    expect(await offer('Explore')).toEqual({ isOffered: true })
  })
})

describe('jobs the mod cannot run', () => {
  test("an agent job whose prompt file cannot be read fails as a read error naming the file, and spawns nothing", async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [agentJob('j1', { prompt: null, promptFile: '/k/in/j1.prompt.txt' })] })
    expect(w.spawns).toEqual([])
    const r = w.resultOf('j1')
    expect(r).toMatchObject({ ok: false, reason: 'error' })
    expect(r.detail).toMatch(/^could not read the job's prompt file \/k\/in\/j1\.prompt\.txt: .*ENOENT/)
  })

  test("a complete job whose prompt or system file cannot be read fails naming that file, and makes no request", async ($, on) => {
    const w = world($, on, { files: { '/k/in/j2.system.txt': { deny: 'EACCES' } } })
    await w.start()
    await w.send({
      jobs: [
        completeJob('j1', { prompt: null, promptFile: '/k/in/j1.prompt.txt' }),
        completeJob('j2', { systemFile: '/k/in/j2.system.txt' }),
      ],
    })
    expect(w.completes).toEqual([])
    expect(w.resultOf('j1')).toMatchObject({ ok: false, reason: 'error', detail: expect.stringMatching(/^could not read the job's prompt file \/k\/in\/j1\.prompt\.txt: /) })
    // Named by the mod, whatever the engine's error says.
    expect(w.resultOf('j2')).toMatchObject({ ok: false, reason: 'error', detail: expect.stringMatching(/^could not read the job's system file \/k\/in\/j2\.system\.txt: .*EACCES/) })
  })

  test('an empty file is a text, not a failure: a spilled system that was empty sends none', async ($, on) => {
    const w = world($, on, { files: { '/k/in/j1.prompt.txt': 'big prompt', '/k/in/j1.system.txt': '' } })
    await w.start()
    await w.send({ jobs: [completeJob('j1', { prompt: null, promptFile: '/k/in/j1.prompt.txt', systemFile: '/k/in/j1.system.txt' })] })
    expect(w.completes).toEqual([{ model: 'haiku', prompt: 'big prompt', maxTokens: 4096 }])
    expect(w.resultOf('j1')).toMatchObject({ ok: true })
  })

  test('an unknown kind is posted as an error naming it', async ($, on) => {
    const w = world($, on)
    await w.start()
    await w.send({ jobs: [{ id: 'j1', kind: 'shell' }] })
    expect(w.resultOf('j1')).toEqual({ ok: false, reason: 'error', detail: 'no job kind shell' })
  })
})

describe('the result POST', () => {
  test('404 counts as delivered: one POST, and the poll goes on', async ($, on) => {
    const w = world($, on, { result: () => ({ status: 404 }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    await w.clock.advance(30_000)
    expect(w.results).toHaveLength(1)
    await w.send({})
    expect(w.isPolling()).toBe(true)
  })

  test('a failed POST is tried three times, 2 s apart; when none lands the mod stops polling', async ($, on) => {
    const w = world($, on, { result: (_, __, n) => (n === 2 ? 'throw' : { status: 500 }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.results).toHaveLength(1)
    await w.clock.advance(2_000)
    expect(w.results).toHaveLength(2)
    await w.clock.advance(2_000)
    expect(w.results).toHaveLength(3)
    await w.clock.advance(30_000)
    expect(w.results).toHaveLength(3)
    // The poll in flight is answered; the loop does not open another.
    const polled = w.polls.length
    await w.send({})
    await w.clock.advance(30_000)
    expect(w.polls).toHaveLength(polled)
  })

  test('a second try that lands ends the retries and the poll goes on', async ($, on) => {
    const w = world($, on, { result: (_, __, n) => (n === 1 ? { status: 500 } : { status: 200 }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    await w.clock.advance(30_000)
    expect(w.results).toHaveLength(2)
    await w.send({})
    expect(w.isPolling()).toBe(true)
  })

  test('the tries share one 10 s window: a POST that hangs through it is the last', async ($, on) => {
    const w = world($, on, { result: () => new Promise<Reply>(() => {}) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    await w.clock.advance(60_000)
    expect(w.results).toHaveLength(1)
    const polled = w.polls.length
    await w.send({})
    expect(w.polls).toHaveLength(polled)
  })

  test('a text past the envelope bound goes to <outDir>/<id>.txt, named by textFile', async ($, on) => {
    const big = 'x'.repeat(3_600_000)
    const w = world($, on, { complete: () => answered(big) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.writes.map((x) => x.path)).toEqual([`${OUT_DIR}/j1.txt`])
    expect(w.writes[0]!.text === big).toBe(true)
    expect(brief(w.resultOf('j1'))).toEqual({ ok: true, text: null, textFile: `${OUT_DIR}/j1.txt`, usage: USAGE })
  })

  test('the bound is on the encoded envelope, not the text: escaping can carry a shorter text past it', async ($, on) => {
    const lines = '\n'.repeat(2_000_000)
    const w = world($, on, { complete: () => answered(lines) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.writes.map((x) => x.path)).toEqual([`${OUT_DIR}/j1.txt`])
    expect(brief(w.resultOf('j1'))).toMatchObject({ text: null, textFile: `${OUT_DIR}/j1.txt` })
  })

  test('a text past the 4 MiB write cap, in UTF-8 bytes, is posted too-large, and nothing is written', async ($, on) => {
    // 3.6 M characters, under the cap; 5.6 M bytes, over it.
    const w = world($, on, { complete: () => answered('é'.repeat(2_000_000) + 'x'.repeat(1_600_000)) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.writes).toEqual([])
    expect(brief(w.resultOf('j1'))).toEqual({ ok: false, reason: 'too-large', detail: 'the reply is 5600000 bytes of text, over the 4 MiB a file write takes' })
  })

  test('without an outDir from hello a large text is too-large', async ($, on) => {
    const w = world($, on, { hello: { status: 200, text: '{"ok":true}' }, complete: () => answered('x'.repeat(3_600_000)) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(w.writes).toEqual([])
    expect(brief(w.resultOf('j1'))).toMatchObject({ ok: false, reason: 'too-large' })
  })

  test('a spill the disk refuses posts an error saying the result could not be written', async ($, on) => {
    const w = world($, on, { complete: () => answered('x'.repeat(3_600_000)), write: () => ({ deny: 'ENOSPC' }) })
    await w.start()
    await w.send({ jobs: [completeJob('j1')] })
    expect(brief(w.resultOf('j1'))).toMatchObject({ ok: false, reason: 'error', detail: expect.stringContaining('the result could not be written') })
  })
})

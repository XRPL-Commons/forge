// Mock stand-in for the Python Forge status API, used only by the e2e harness
// (tests/e2e/run.mjs). Serves canned JSON fixtures from tests/e2e/fixtures/,
// switchable per-scenario, and records every request it receives so specs can
// assert on what the app actually sent through the Nitro proxy.
import http from 'node:http'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { withAttentionKinds } from './attention-kind.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FIXTURES_DIR = path.join(__dirname, 'fixtures')

const HOST = process.env.FORGE_MOCK_HOST || '127.0.0.1'
const PORT = Number(process.env.FORGE_MOCK_PORT || 8788)
const ORIGIN = `http://${HOST}:${PORT}`

// Stands in for Python's secrets.token_urlsafe(32) on the GitHub callback:
// 43 url-safe characters, inside the [A-Za-z0-9_-]{32,128} range the
// contracts (§1, cookie forge_setup) call valid.
const CALLBACK_COOKIE = 'synthetic-cookie-Kf3n0pQ7rLxW9sVbZt2yHdE'

let scenario = 'default'
let sequence = 1
let requests = []
// Like the real server, a saved runtime is what later factory reads return.
let savedSettings = null
let savedFactory = null
let savedRepositoryView = null
let savedIssue = null
let savedIssueList = null
let connectedAgents = []
// Coding-account health served on every /api/factory read (POST /__accounts),
// so a spec can take an account down on any scenario's board.
let codingAccounts = null
let conversation = { messages: [], status: 'idle', reason: null, before: null, partial: '', origin: null }
// Questions whose first reply already failed: "(fail)" fails once, then answers.
let failedQuestions = new Set()
// The reply in progress, scripted on the clock like the real presence model:
// Forge looks for someone available, an agent joins and works, then answers
// or leaves. Each poll reads where the script has got to.
//   plain       finding → Ember joins, three activity lines → answer
//   "(hold)"    finding → Ember joins and keeps working until stopped
//   "(fail)"    first time: Ember joins, then runs out of time
//   "(auth)"    Bolt joins, cannot sign in, Ember joins instead and answers
let turn = null
// A spec can hold a transient presence phase while it checks the rendered UI.
// null keeps the ordinary wall-clock script used by the other scenarios.
let discussionElapsed = null
const EMBER = { provider: 'claude', model: 'claude-sonnet-4-6', name: 'Ember', label: 'Claude' }
const BOLT = { provider: 'codex', model: null, name: 'Bolt', label: 'Codex' }
const LINES = ['Getting started', 'Reading issue #610', 'Searching the repository for `producer`']
const ANSWER = 'The last attempt failed because the shared type is incorrect. Fix that producer first. [View attempt](/issues/1:610?job=example)'
// "(long)": an answer well past six lines, which the chat folds behind Show more.
const LONG_ANSWER = ['The last attempt failed because the shared type is incorrect.', '',
  ...Array.from({ length: 9 }, (_, i) => `- Finding ${i + 1}: the consumer in module ${i + 1} still reads the old field.`), '',
  'The final line of the long answer.'].join('\n')

function startTurn(question, kind) {
  turn = { question, kind, started: Date.now() }
  Object.assign(conversation, { status: 'queued', reason: 'Looking for someone available', partial: '',
    presence: { phase: 'finding', waiting_since: Date.now() / 1000, code: 'busy', reason: 'Another conversation is using the discussion worker; one runs at a time' } })
}

function joined(agent, since, t, instead = null) {
  const at = turn.started / 1000 + since / 1000
  const lines = LINES.filter((_, i) => t >= since + i * 700).map((text, i) => ({ at: at + i * 0.7, text }))
  // Like Forge: answering from the saved evidence until it searches the repository.
  const mode = lines.some(line => line.text.startsWith('Searching the repository')) ? 'code' : 'evidence'
  return { phase: 'joined', agent, joined_at: at, activity: lines, instead_of: instead, mode }
}

function leave(reason, text, agent, extra = {}) {
  conversation.messages.push({ id: `left-${conversation.messages.length + 1}`, sequence: conversation.messages.length + 1, role: 'system', event: 'left',
    reason, text, agent, reply_to: turn.question, at: Date.now() / 1000, ...extra })
}

function answer(agent, instead = null) {
  const lines = LINES.map((text, i) => ({ at: turn.started / 1000 + 1 + i, text }))
  conversation.messages.push({ id: `11111111-2222-4333-8444-${555555555555 + conversation.messages.filter(m => m.role === 'assistant').length}`, sequence: conversation.messages.length + 1, role: 'assistant',
    text: turn.kind === 'long' ? LONG_ANSWER : ANSWER, agent, instead_of: instead, activity: [...lines, { at: turn.started / 1000 + 4, text: 'Writing the answer' }],
    proposal: { action: 'retry', reason: 'Correct the shared producer type before updating consumers.' } })
  Object.assign(conversation, { status: 'idle', reason: null, partial: '', presence: { phase: 'answered', agent, at: Date.now() / 1000 } })
  turn = null
}

function advance() {
  if (!turn) return
  const t = discussionElapsed ?? Date.now() - turn.started
  if (turn.kind === 'auth') {
    if (t < 1000) return
    if (t < 4000) return Object.assign(conversation, { status: 'running', presence: joined(BOLT, 1000, t) })
    if (!turn.left) {
      turn.left = true
      leave('auth', "Bolt can't sign in: the Codex login expired. Reconnect it in Settings → Coding accounts.", BOLT,
        { link: { to: '/settings/harnesses', label: 'Settings → Coding accounts' }, retrying: true })
    }
    if (t < 6500) return Object.assign(conversation, { status: 'queued', presence: { phase: 'finding', waiting_since: turn.started / 1000 + 4, code: 'switching', reason: 'Finding someone else to answer', instead_of: BOLT } })
    if (t < 9000) return Object.assign(conversation, { status: 'running', presence: joined(EMBER, 6500, t, BOLT) })
    return answer(EMBER, BOLT)
  }
  if (t < 1200) return
  if (turn.kind === 'fail' && t >= 3000) {
    leave('timeout', 'Ember ran out of time after 10 min', EMBER)
    Object.assign(conversation, { status: 'unavailable', reason: 'Ember ran out of time after 10 min', partial: '',
      presence: { phase: 'left', reason: 'timeout', text: 'Ember ran out of time after 10 min', agent: EMBER, at: Date.now() / 1000 } })
    turn = null
    return
  }
  // "(slow)": Ember has been reading the code for over 90 s, until stopped.
  if (turn.kind === 'slow') {
    const working = joined(EMBER, 1200, t)
    return Object.assign(conversation, { status: conversation.status === 'cancelling' ? 'cancelling' : 'running',
      presence: { ...working, joined_at: Date.now() / 1000 - 95, mode: 'code' } })
  }
  if (turn.kind === 'hold' || t < 6000) return Object.assign(conversation, { status: conversation.status === 'cancelling' ? 'cancelling' : 'running', presence: joined(EMBER, 1200, t) })
  answer(EMBER)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    req.on('data', (chunk) => chunks.push(chunk))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function parseBody(raw, contentType) {
  if (!raw) return null
  const type = (contentType || '').split(';')[0].trim()
  try {
    if (type === 'application/json') return JSON.parse(raw)
    if (type === 'application/x-www-form-urlencoded') return Object.fromEntries(new URLSearchParams(raw))
    return raw
  } catch {
    return raw
  }
}

// Fixture naming rule (task-5-brief §Step 4/5/6): path segments after /api/
// join with '-' (`/api/factory/output` -> `factory-output`). A query
// parameter that selects among several fixtures for the same path appends
// `.<param>` (`/api/issues?issue=` -> `issues.issue`); a parameter that does
// not change which fixture applies (e.g. `/api/factory/output?id=`) does
// not. Rather than hard-code which query keys matter per endpoint, this
// resolves by trying the most specific candidate name (base + each query
// key, in order) before the bare base name, and picks the first candidate
// that actually has a fixture file — so `?id=` falls through to the base
// file exactly when no `<base>.id.json` fixture exists.
function candidateNames(base, query) {
  const keys = Object.keys(query)
  return [...keys.map((key) => `${base}.${key}`), base]
}

function readFixtureFile(dir, name) {
  const file = path.join(FIXTURES_DIR, dir, `${name}.json`)
  if (!existsSync(file)) return undefined
  return JSON.parse(readFileSync(file, 'utf8').replaceAll('http://127.0.0.1:8788', ORIGIN))
}

// GET /api/<x>: fixtures/<scenario>/<name>.json when present, else
// fixtures/default/<name>.json.
function resolveGetFixture(base, query) {
  const names = candidateNames(base, query)
  for (const name of names) {
    const found = readFixtureFile(scenario, name)
    if (found !== undefined) return found
  }
  for (const name of names) {
    const found = readFixtureFile('default', name)
    if (found !== undefined) return found
  }
  return undefined
}

// POST fixture override: fixtures/<scenario>/<name>.post.json only (no
// fallback to default/ — the scenario IS 'default' unless switched, so a
// default/<name>.post.json fixture is still picked up when no other
// scenario has been selected).
function resolvePostFixture(base, query) {
  const names = candidateNames(base, query).map((name) => `${name}.post`)
  for (const name of names) {
    const found = readFixtureFile(scenario, name)
    if (found !== undefined) return found
  }
  return undefined
}

// Injects controls_token: 'synthetic' into every object response, except a
// fixture whose top level is already {"ok": false, ...} — that shape is
// served verbatim so the {ok:false, reason} path is exercised untouched.
function withToken(payload) {
  if (payload && typeof payload === 'object' && !Array.isArray(payload) && payload.ok !== false) {
    return { ...payload, controls_token: 'synthetic' }
  }
  return payload
}

// /api/factory?section=completed&limit=N&before=<cursor>: one page of the
// fixture's completed issues, as forge/factory/control.py completed_page()
// answers it — newest first by completion time, then key; the cursor names
// the last row returned (`<at>:<key>`), and `next` is null on the last page.
function completedAt(issue) {
  const events = (issue.history ?? []).filter(event => event.kind === 'merged' || event.kind === 'cancelled')
  return events.length ? events[events.length - 1].at : 0
}
function completedPage(fixture, query) {
  const limit = Number(query.limit)
  const later = (a, b) => (completedAt(b) - completedAt(a)) || (a.key < b.key ? 1 : a.key > b.key ? -1 : 0)
  let rows = (fixture.issues ?? []).filter(issue => ['merged', 'cancelled'].includes(issue.state) && !issue.active).sort(later)
  if (query.before) {
    const split = query.before.indexOf(':')
    const at = Number(query.before.slice(0, split)), key = query.before.slice(split + 1)
    rows = rows.filter(issue => completedAt(issue) < at || (completedAt(issue) === at && issue.key < key))
  }
  const page = rows.slice(0, limit).map(issue => ({ ...issue, completed_at: completedAt(issue) || null }))
  const last = page[page.length - 1]
  return { ...fixture, issues: page, jobs: [], next: rows.length > limit ? `${completedAt(last)}:${last.key}` : null }
}

function record(req, pathname, query, body) {
  requests.push({ method: req.method, path: pathname, query, body: body ?? null, cookie: req.headers.cookie ?? null, etag: req.headers['if-none-match'] ?? null })
}

function send(res, status, payload, headers = {}) {
  const data = Buffer.from(JSON.stringify(payload))
  const timed = res.req.method === 'GET' && ['/api/factory', '/api/issues', '/api/delivery'].includes(new URL(res.req.url, ORIGIN).pathname)
  if (timed) headers = { ...headers, 'server-timing': 'projection;dur=1, cache_miss;dur=2, request;dur=3' }
  if (res.req.method === 'GET' && status === 200) {
    const etag = '"' + createHash('sha256').update(data).digest('hex') + '"'
    headers = { ...headers, etag, 'cache-control': 'private, no-store' }
    if (res.req.headers['if-none-match'] === etag) {
      if (timed) headers['server-timing'] = 'cache_validated;dur=0.5, conditional;dur=0.1, request;dur=1'
      res.writeHead(304, headers); return res.end()
    }
  }
  res.writeHead(status, { 'content-type' : 'application/json; charset=utf-8', 'content-length': String(data.length), ...headers })
  res.end(data)
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, ORIGIN)
  const pathname = url.pathname
  const query = Object.fromEntries(url.searchParams)

  // Scenario 'down' simulates the upstream being unreachable: every GET to
  // /api/** has its socket destroyed instead of answered, so the Nitro
  // proxy's fetch() rejects and the app sees a 502.
  if (scenario === 'down' && req.method === 'GET' && pathname.startsWith('/api/')) {
    req.socket.destroy()
    return
  }

  const rawBody = req.method === 'POST' ? await readBody(req) : ''
  const body = parseBody(rawBody, req.headers['content-type'])
  if (req.method === 'POST') sequence++

  if (req.method === 'GET' && pathname === '/__requests') {
    return send(res, 200, requests)
  }

  // Observations belong to the mock server, so SSR, ordinary reads and the
  // committed change feed see the same state. Browser routing cannot do this.
  if (req.method === 'POST' && pathname === '/__issue') {
    const current = savedIssue?.issue.key === body?.key ? savedIssue : resolveGetFixture('issues', { issue: body?.key })
    if (!current?.issue || current.issue.key !== body?.key) return send(res, 400, { ok: false, reason: 'Unknown fixture issue' })
    savedIssue = { ...current, ...(body.envelope || {}), issue: { ...current.issue, ...(body.patch || {}) } }
    if (body.list) savedIssueList = { ...resolveGetFixture('issues', {}), issues: [savedIssue.issue], total: 1 }
    else if (savedIssueList) savedIssueList = { ...savedIssueList, issues: savedIssueList.issues.map(row => row.key === body.key ? savedIssue.issue : row) }
    return send(res, 200, savedIssue)
  }

  if (req.method === 'POST' && pathname === '/__accounts') {
    codingAccounts = body && typeof body === 'object' && Array.isArray(body.accounts) ? body.accounts : null
    return send(res, 200, { ok: true })
  }

  if (req.method === 'POST' && pathname === '/__discussion-time') {
    const elapsed = body?.elapsed
    if (elapsed !== null && (!Number.isFinite(elapsed) || elapsed < 0)) {
      return send(res, 400, { ok: false })
    }
    discussionElapsed = elapsed
    advance()
    return send(res, 200, { ok: true })
  }

  if (req.method === 'POST' && pathname === '/__scenario') {
    const name = body && typeof body === 'object' ? body.name : undefined
    if (typeof name === 'string' && name) scenario = name
    requests = []
    savedSettings = null
    savedFactory = null
    savedRepositoryView = null
    savedIssue = null
    savedIssueList = null
    codingAccounts = null
    connectedAgents = []
    conversation = { messages: [], status: 'idle', reason: null, before: null, partial: '', origin: null }
    failedQuestions = new Set()
    turn = null
    discussionElapsed = null
    return send(res, 200, { ok: true })
  }

  if (pathname === '/api/connections') {
    record(req, pathname, query, body)
    return send(res, 200, withToken({ enabled: scenario === 'auth-agents', connections: connectedAgents,
      reason: 'Configure HTTPS and operator sign-in to connect an agent.' }))
  }
  if (pathname === '/api/connections/preview') {
    record(req, pathname, query, body)
    if (body.code !== 'BCDF-2345') return send(res, 200, { ok: false, reason: 'This code expired or has already been used.' })
    return send(res, 200, withToken({ name: 'Local agent', user_code: body.code, expires: 1790682000, origin: 'https://forge.example',
      capabilities: ['repositories:read', 'repositories:prepare', 'repositories:activate'],
      scope: { repositories: [{ id: 321, name: 'example/project' }], owners: [{ id: 88, login: 'example' }] } }))
  }
  if (pathname === '/api/connections/decide') {
    record(req, pathname, query, body)
    if (body.decision === 'approve') connectedAgents.push({ id: 'synthetic-client', name: 'Local agent', operator: { id: 123, login: 'forge-operator' },
      created: 1790680000, expires: 1798456000, last_used: null, status: 'active', capabilities: body.capabilities, scope: body.scope })
    return send(res, 200, withToken({ status: body.decision === 'approve' ? 'approved' : 'denied' }))
  }
  if (pathname === '/api/connections/revoke') {
    record(req, pathname, query, body)
    connectedAgents.find(c => c.id === body.id).status = 'revoked'
    return send(res, 200, withToken({ revoked: true }))
  }
  if (pathname === '/api/auth/session') {
    record(req, pathname, query, body)
    const enabled = scenario.startsWith('auth-')
    const signed = enabled && (req.headers.cookie ?? '').includes('synthetic_operator=yes')
    return send(res, 200, { ok: true, enabled, operator: signed ? { id: 123, login: 'forge-operator' } : null,
      ...(signed ? { controls_token: 'synthetic' } : {}) })
  }
  if (pathname === '/api/auth/logout') {
    record(req, pathname, query, body)
    return send(res, 200, { ok: true }, { 'set-cookie': 'synthetic_operator=; Path=/; Max-Age=0' })
  }
  if (scenario === 'auth-expired' && pathname.startsWith('/api/')) {
    record(req, pathname, query, body)
    return send(res, 401, { ok: false, reason: 'Sign in again' })
  }

  if (pathname === '/api/issues/discussion') {
    record(req, pathname, query, body)
    if (req.method === 'POST') {
      let request = 'discussion-decision'
      if (body.action === 'send') {
        const text = body.message.trim()
        // Like Forge: the same words as the unanswered question are never a
        // second copy; after a failed reply they are Try again.
        const asked = conversation.messages.findLast(m => m.role !== 'system')
        if (asked?.role === 'user' && asked.text === text) {
          request = asked.id
          if (!turn) startTurn(asked.id, 'answer')
        } else {
          conversation.messages.push({ id: body.request_id, sequence: conversation.messages.length + 1, role: 'user', text })
          const failing = text.includes('(fail)') && !failedQuestions.has(text)
          if (failing) failedQuestions.add(text)
          startTurn(body.request_id, text.includes('(hold)') ? 'hold' : text.includes('(slow)') ? 'slow' : text.includes('(long)') ? 'long' : text.includes('(auth)') ? 'auth' : failing ? 'fail' : 'answer')
        }
      } else if (body.action === 'cancel') {
        const agent = conversation.presence?.agent ?? null
        const text = agent ? `You stopped ${agent.name}` : 'You stopped the request'
        if (turn) leave('cancelled', text, agent)
        turn = null
        Object.assign(conversation, { status: 'cancelled', reason: text, partial: '', presence: { phase: 'left', reason: 'cancelled', text, agent, at: Date.now() / 1000 } })
      }
      else if (body.action === 'apply') conversation.messages.find(m => m.id === body.message_id).receipt = 'discussion-decision'
      return send(res, 200, withToken({ ok: true, request }))
    }
    advance()
    return send(res, 200, withToken({ ok: true, ...conversation }))
  }

  // GitHub App manifest hand-off stand-in: accepts any content type (the
  // real browser flow submits application/x-www-form-urlencoded), skips the
  // action guard entirely, records the request and redirects to /onboarding.
  if (req.method === 'POST' && pathname === '/__manifest') {
    record(req, pathname, query, body)
    res.writeHead(303, { location: '/onboarding' })
    return res.end()
  }

  // GitHub OAuth callback stand-in: always succeeds and sets the setup
  // cookie so the smoke spec can assert it reaches the browser through the
  // Nitro proxy's cookie pass-through.
  if (req.method === 'GET' && pathname === '/onboarding/github/callback') {
    record(req, pathname, query, body)
    res.writeHead(303, { location: '/onboarding', 'set-cookie': `forge_setup=${CALLBACK_COOKIE}; Path=/; HttpOnly` })
    return res.end()
  }

  if (!pathname.startsWith('/api/')) {
    record(req, pathname, query, body)
    return send(res, 404, { ok: false, reason: 'missing' })
  }

  const segments = pathname.slice('/api/'.length).split('/').filter(Boolean)
  const base = segments.join('-')

  if (req.method === 'GET' && base === 'factory-changes') {
    record(req, pathname, query, body)
    const before = Number((query.since || '').split(':')[1])
    // Short idle waits keep deterministic UI tests quick; mutations still wake the
    // feed by their sequence, and the response shape matches the production endpoint.
    for (let left = 20; before === sequence && left && !res.destroyed; left--) await new Promise(resolve => setTimeout(resolve, 50))
    if (res.destroyed) return
    const board = withAttentionKinds(savedFactory || resolveGetFixture('factory', { section: 'board' }))
    if (board.ok === false) return send(res, 200, board)
    if (codingAccounts) board.coding_accounts = codingAccounts
    const changed = before !== sequence
    const detail = savedIssue || resolveGetFixture('issues', { issue: '1:610' })
    const issues = {}
    if (changed && detail?.issue) {
      const { body, history, reviews, reviewers, default_reviewers, reviewers_requested, ...summary } = detail.issue
      issues[summary.key] = summary
    }
    return send(res, 200, withToken({ cursor: `mock:${sequence}`, at: Date.now() / 1000, events: changed ? [{ sequence, issue_id: detail?.issue?.key || null, unit_id: null, job_id: null, kind: 'observed', at: Date.now() / 1000 }] : [],
      issues, board: withToken({ ...board, cursor: `mock:${sequence}` }), resync: false }))
  }

  if (req.method === 'GET') {
    record(req, pathname, query, body)
    let fixture = base === 'settings' && savedSettings ? savedSettings
      : base === 'factory' && savedFactory ? savedFactory
      : base === 'issues' && query.issue && savedIssue?.issue.key === query.issue ? savedIssue
      : base === 'issues' && !query.issue && savedIssueList ? savedIssueList
      : base === 'repositories' && savedRepositoryView?.repositories.some(row => String(row.id) === query.repo_id) ? savedRepositoryView
        : resolveGetFixture(base, query)
    if (fixture === undefined) return send(res, 404, { ok: false, reason: 'missing' })
    if (base === 'factory') fixture = { ...withAttentionKinds(fixture), cursor: `mock:${sequence}` }
    if (base === 'issues' && query.issue && fixture.issue) {
      const scoped = savedFactory || resolveGetFixture('factory', { issue: query.issue })
      fixture = { ...fixture, cursor: `mock:${sequence}`, jobs: (scoped?.jobs || []).filter(job => job.issue_key === fixture.issue.key),
        factory_issue: (scoped?.issues || []).find(issue => issue.key === fixture.issue.key) || null,
        deployments: scoped?.deployments || {} }
    }
    if (base === 'factory' && codingAccounts && fixture.ok !== false) fixture = { ...fixture, coding_accounts: codingAccounts }
    if (base === 'factory' && query.section === 'completed' && query.limit && fixture.ok !== false) {
      return send(res, 200, withToken(completedPage(fixture, query)))
    }
    const headers = {}
    if (base === 'repositories-ruleset') {
      headers['content-disposition'] = `attachment; filename="forge-review-rules-${query.repo_id ?? ''}.json"`
    }
    return send(res, 200, withToken(fixture), headers)
  }

  if (req.method === 'POST') {
    record(req, pathname, query, body)
    const guarded = req.headers['x-forge-action'] === 'synthetic'
      && req.headers.origin === ORIGIN
      && req.headers['sec-fetch-site'] === 'same-origin'
    if (!guarded) return send(res, 403, { ok: false, reason: 'Action rejected by mock' })
    const fixture = resolvePostFixture(base, query)
    if (base === 'repositories-disable' && fixture === undefined) {
      savedSettings = resolveGetFixture('settings', {})
      savedSettings.revision = 'settings-rev-disabled'
      for (const rows of [savedSettings.repositories, savedSettings.config.repositories]) {
        const row = rows.find(row => row.id === body.repo_id)
        if (row) row.enabled = false
      }
      return send(res, 200, withToken({ revision: savedSettings.revision,
        repositories: savedSettings.repositories.filter(row => row.id === body.repo_id) }))
    }
    if (base === 'runtime' && fixture?.runtime) savedFactory = fixture
    if (['repositories-environment', 'repositories-enable'].includes(base) && fixture?.repositories) savedRepositoryView = fixture
    if (base === 'issues-action' && fixture?.issue) savedIssue = fixture
    if (fixture !== undefined) return send(res, 200, withToken(fixture))
    return send(res, 200, { ok: true })
  }

  record(req, pathname, query, body)
  return send(res, 404, { ok: false, reason: 'missing' })
})

server.listen(PORT, HOST, () => {
  console.log(`mock-api listening on http://${HOST}:${PORT}`)
})

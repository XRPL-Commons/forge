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
let requests = []
// Like the real server, a saved runtime is what later factory reads return.
let savedSettings = null
let savedFactory = null
let savedRepositoryView = null
let savedIssue = null
let connectedAgents = []
let conversation = { messages: [], status: 'idle', reason: null, before: null, partial: '', origin: null }

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
  if (res.req.method === 'GET' && status === 200) {
    const etag = '"' + createHash('sha256').update(data).digest('hex') + '"'
    headers = { ...headers, etag, 'cache-control': 'private, no-store' }
    if (res.req.headers['if-none-match'] === etag) { res.writeHead(304, headers); return res.end() }
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

  if (req.method === 'GET' && pathname === '/__requests') {
    return send(res, 200, requests)
  }

  if (req.method === 'POST' && pathname === '/__scenario') {
    const name = body && typeof body === 'object' ? body.name : undefined
    if (typeof name === 'string' && name) scenario = name
    requests = []
    savedSettings = null
    savedFactory = null
    savedRepositoryView = null
    savedIssue = null
    connectedAgents = []
    conversation = { messages: [], status: 'idle', reason: null, before: null, partial: '', origin: null }
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
      if (body.action === 'send') {
        conversation.messages.push({ id: body.request_id, sequence: conversation.messages.length + 1, role: 'user', text: body.message })
        // A message containing "(hold)" leaves the reply in progress so a
        // spec can exercise stopping it; anything else is answered at once.
        if (body.message.includes('(hold)')) Object.assign(conversation, { status: 'running', reason: 'Agent is reading the saved evidence', partial: 'Reading the saved evidence' })
        else conversation.messages.push({ id: '11111111-2222-4333-8444-555555555555', sequence: conversation.messages.length + 1, role: 'assistant',
          text: 'The last attempt failed because the shared type is incorrect. Fix that producer first. [View attempt](/issues/1:610?job=example)',
          proposal: { action: 'retry', reason: 'Correct the shared producer type before updating consumers.' } })
      } else if (body.action === 'cancel') Object.assign(conversation, { status: 'cancelled', reason: 'Reply stopped', partial: '' })
      else if (body.action === 'apply') conversation.messages.find(m => m.id === body.message_id).receipt = 'discussion-decision'
      return send(res, 200, withToken({ ok: true, request: 'discussion-decision' }))
    }
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

  if (req.method === 'GET') {
    record(req, pathname, query, body)
    let fixture = base === 'settings' && savedSettings ? savedSettings
      : base === 'factory' && savedFactory ? savedFactory
      : base === 'issues' && query.issue && savedIssue?.issue.key === query.issue ? savedIssue
      : base === 'repositories' && savedRepositoryView?.repositories.some(row => String(row.id) === query.repo_id) ? savedRepositoryView
        : resolveGetFixture(base, query)
    if (fixture === undefined) return send(res, 404, { ok: false, reason: 'missing' })
    if (base === 'factory') fixture = withAttentionKinds(fixture)
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

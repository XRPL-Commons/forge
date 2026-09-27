// Mock stand-in for the Python Forge status API, used only by the e2e harness
// (tests/e2e/run.mjs). Serves canned JSON fixtures from tests/e2e/fixtures/,
// switchable per-scenario, and records every request it receives so specs can
// assert on what the app actually sent through the Nitro proxy.
import http from 'node:http'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

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
let savedFactory = null
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
  return JSON.parse(readFileSync(file, 'utf8'))
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

function record(req, pathname, query, body) {
  requests.push({ method: req.method, path: pathname, query, body: body ?? null, cookie: req.headers.cookie ?? null })
}

function send(res, status, payload, headers = {}) {
  const data = Buffer.from(JSON.stringify(payload))
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': String(data.length), ...headers })
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
    savedFactory = null
    conversation = { messages: [], status: 'idle', reason: null, before: null, partial: '', origin: null }
    return send(res, 200, { ok: true })
  }

  if (pathname === '/api/issues/discussion') {
    record(req, pathname, query, body)
    if (req.method === 'POST') {
      if (body.action === 'send') {
        conversation.messages.push({ id: body.request_id, sequence: 1, role: 'user', text: body.message })
        conversation.messages.push({ id: '11111111-2222-4333-8444-555555555555', sequence: 2, role: 'assistant',
          text: 'The last attempt failed because the shared type is incorrect. Fix that producer first. [View attempt](/issues/1:610?job=example)',
          proposal: { action: 'retry', reason: 'Correct the shared producer type before updating consumers.' } })
      } else if (body.action === 'apply') conversation.messages[1].receipt = 'discussion-decision'
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
    const fixture = base === 'factory' && savedFactory && !Object.keys(query).length ? savedFactory : resolveGetFixture(base, query)
    if (fixture === undefined) return send(res, 404, { ok: false, reason: 'missing' })
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
    if (base === 'runtime' && fixture?.runtime) savedFactory = fixture
    if (fixture !== undefined) return send(res, 200, withToken(fixture))
    return send(res, 200, { ok: true })
  }

  record(req, pathname, query, body)
  return send(res, 404, { ok: false, reason: 'missing' })
})

server.listen(PORT, HOST, () => {
  console.log(`mock-api listening on http://${HOST}:${PORT}`)
})

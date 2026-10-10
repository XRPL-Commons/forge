// End-to-end runner: builds the app if needed, starts the mock Forge API and
// the built Nuxt app against it, then runs every tests/e2e/*.spec.mjs file
// with playwright-core against a real (or CI-provided) Chromium.
//
// Usage: node tests/e2e/run.mjs
//
// Env:
//   FORGE_E2E_MOCK_PORT / FORGE_E2E_APP_PORT  Override ports for concurrent checkouts.
//   FORGE_BROWSER_PATH      Chromium executable to launch, e.g. /usr/bin/chromium
//                            in the runner image. Empty/unset falls back to
//                            playwright-core's own bundled Chromium (CI installs
//                            it with `npx playwright-core install chromium`).
//   FORGE_PLAYWRIGHT_MODULE Module specifier/path to load `chromium` from
//                            (default: the project's own playwright-core).
//   FORGE_CHROMIUM_SANDBOX  '0' launches with chromiumSandbox:false, for local
//                            runs on hosts where the Chromium sandbox can't be
//                            set up (e.g. some macOS setups). Any other value,
//                            or unset, keeps the default (chromiumSandbox:true),
//                            which CI and the runner image use.
import { spawn, spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { readdir } from 'node:fs/promises'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const require = createRequire(import.meta.url)
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const UI_ROOT = path.resolve(__dirname, '..', '..')

const MOCK_HOST = '127.0.0.1'
const MOCK_PORT = Number(process.env.FORGE_E2E_MOCK_PORT || 8788)
const MOCK_BASE = `http://${MOCK_HOST}:${MOCK_PORT}`
const APP_HOST = '127.0.0.1'
const APP_PORT = Number(process.env.FORGE_E2E_APP_PORT || 3999)
const APP_BASE = `http://${APP_HOST}:${APP_PORT}`

function log(...args) { console.log('[e2e]', ...args) }

function waitForPort(host, port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    function attempt() {
      const socket = net.connect({ host, port }, () => { socket.end(); resolve() })
      socket.on('error', () => {
        socket.destroy()
        if (Date.now() > deadline) return reject(new Error(`Timed out waiting for ${host}:${port}`))
        setTimeout(attempt, 150)
      })
    }
    attempt()
  })
}

function waitForExit(child) {
  return new Promise((resolve) => child.once('exit', (code, signal) => resolve({ code, signal })))
}

async function stop(child) {
  if (!child || child.exitCode !== null) return
  child.kill('SIGTERM')
  await Promise.race([waitForExit(child), new Promise((resolve) => setTimeout(resolve, 3000))])
  if (child.exitCode === null) child.kill('SIGKILL')
}

async function main() {
  const outputDir = process.env.FORGE_E2E_OUTPUT_DIR || path.join(UI_ROOT, '.output')
  if (!existsSync(outputDir)) {
    log('.output missing, building…')
    const build = spawnSync('npm', ['run', 'build'], { cwd: UI_ROOT, stdio: 'inherit' })
    if (build.status !== 0) { log('build failed'); process.exit(1) }
  }

  log(`starting mock-api on ${MOCK_BASE}`)
  const mock = spawn(process.execPath, [path.join(__dirname, 'mock-api.mjs')], {
    cwd: __dirname,
    env: { ...process.env, FORGE_MOCK_HOST: MOCK_HOST, FORGE_MOCK_PORT: String(MOCK_PORT) },
    stdio: 'inherit',
  })

  let app
  let browser
  let failures = 0
  try {
    await waitForPort(MOCK_HOST, MOCK_PORT)

    log(`starting app on ${APP_BASE}`)
    app = spawn(process.execPath, [path.join(outputDir, 'server', 'index.mjs')], {
      cwd: UI_ROOT,
      env: {
        ...process.env,
        NITRO_FORGE_API: MOCK_BASE,
        NUXT_PUBLIC_POLL_INTERVAL: '300',
        NITRO_HELP_DOC: 'tests/e2e/fixtures/help.md',
        PORT: String(APP_PORT),
        HOST: APP_HOST,
        NITRO_PORT: String(APP_PORT),
        NITRO_HOST: APP_HOST,
      },
      stdio: 'inherit',
    })
    await waitForPort(APP_HOST, APP_PORT)

    const playwrightSpecifier = process.env.FORGE_PLAYWRIGHT_MODULE || 'playwright-core'
    const { chromium } = require(playwrightSpecifier)
    // Empty/unset FORGE_BROWSER_PATH falls back to playwright-core's own
    // bundled Chromium (used in CI, after `playwright-core install
    // chromium`); the runner image and local runs set it explicitly.
    const executablePath = process.env.FORGE_BROWSER_PATH?.trim() || undefined
    const chromiumSandbox = process.env.FORGE_CHROMIUM_SANDBOX === '0' ? false : true
    log(`launching chromium (${executablePath || '(playwright bundled)'}, sandbox=${chromiumSandbox})`)
    browser = await chromium.launch({
      headless: true,
      chromiumSandbox,
      ...(executablePath ? { executablePath } : {}),
    })

    async function scenario(name) {
      await fetch(`${MOCK_BASE}/__scenario`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name }),
      })
    }
    async function observeIssue(key, patch = {}, { envelope = {}, list = false } = {}) {
      const response = await fetch(`${MOCK_BASE}/__issue`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ key, patch, envelope, list }),
      })
      if (!response.ok) throw new Error(`Invalid mock issue observation: ${key}`)
      return response.json()
    }
    async function accounts(list) {
      await fetch(`${MOCK_BASE}/__accounts`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ accounts: list }),
      })
    }
    async function requests() {
      const res = await fetch(`${MOCK_BASE}/__requests`)
      return res.json()
    }
    async function observeDiscussion(message_id, action_state) {
      const res = await fetch(`${MOCK_BASE}/__discussion-action`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message_id, action_state }),
      })
      if (!res.ok) throw new Error('Unknown mock discussion message')
    }
    async function discussionTime(elapsed) {
      const res = await fetch(`${MOCK_BASE}/__discussion-time`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ elapsed }),
      })
      if (!res.ok) throw new Error('Invalid mock discussion time')
    }

    const entries = await readdir(__dirname)
    const selected = new Set((process.env.FORGE_E2E_SPEC || '').split(',').filter(Boolean))
    const specNames = entries.filter((name) => name.endsWith('.spec.mjs') && (!selected.size || selected.has(name))).sort()
    if (specNames.length === 0) log('no *.spec.mjs files found')

    for (const specName of specNames) {
      await scenario('default')
      const context = await browser.newContext()
      const page = await context.newPage()
      const name = specName.replace(/\.spec\.mjs$/, '')
      try {
        const mod = await import(pathToFileURL(path.join(__dirname, specName)).href)
        await mod.default(page, { base: APP_BASE, scenario, requests, accounts, discussionTime, observeIssue, observeDiscussion })
        log(`PASS ${name}`)
      } catch (error) {
        failures += 1
        log(`FAIL ${name}`)
        console.error(error)
      } finally {
        await context.close()
      }
    }
  } finally {
    if (browser) await browser.close()
    await stop(app)
    await stop(mock)
  }

  process.exit(failures ? 1 : 0)
}

main().catch((error) => { console.error(error); process.exit(1) })

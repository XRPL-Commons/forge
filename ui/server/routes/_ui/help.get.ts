// GET /_ui/help — resolves and renders the setup guide for /setup-help.
// Lives under /_ui/ (not /api/) for the same reason as _ui/markdown.post.ts:
// /api and /api/* are proxied straight to the Forge status service
// (server/middleware/forge-proxy.ts + server/utils/proxy.ts's proxyPaths()),
// and the `_ui` prefix stays clear of that proxy. This route does its own
// I/O (reading the resolved document) and its own caching; server/utils/help.ts
// stays pure (renderHelp is markdown in, {html, toc} out).
import { accessSync, constants as fsConstants, readFileSync } from 'node:fs'
import path from 'node:path'
import { renderHelp, resolveHelpDocPath, type HelpDoc } from '../../utils/help'

const CACHE_TTL_MS = 60_000

let cache: { at: number; doc: HelpDoc } | null = null

function isReadable(candidate: string): boolean {
  try {
    accessSync(candidate, fsConstants.R_OK)
    return true
  } catch {
    return false
  }
}

// The repo default candidate. `process.cwd()` is `ui/` when Nitro runs (the
// e2e harness spawns the built server with that as its cwd), matching the
// `../docs/onboarding.md` the runtimeConfig default documents.
function repoDefaultPath(): string {
  return path.resolve(process.cwd(), '../docs/onboarding.md')
}

export default defineEventHandler((event) => {
  const now = Date.now()
  if (cache && now - cache.at < CACHE_TTL_MS) return cache.doc

  const nitroHelpDoc = process.env.NITRO_HELP_DOC
    ? path.resolve(process.cwd(), process.env.NITRO_HELP_DOC)
    : null
  const forgeRoot = process.env.FORGE_ROOT?.trim() || null

  const docPath = resolveHelpDocPath({ nitroHelpDoc, forgeRoot, repoDefault: repoDefaultPath() }, isReadable)
  if (!docPath) {
    setResponseStatus(event, 404)
    return { html: '', toc: [] } satisfies HelpDoc
  }

  const source = readFileSync(docPath, 'utf8')
  const doc = renderHelp(source)
  cache = { at: now, doc }
  return doc
})

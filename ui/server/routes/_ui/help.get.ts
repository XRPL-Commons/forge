// GET /_ui/help — resolves and renders the setup guide for /setup-help.
// Lives under /_ui/ (not /api/) for the same reason as _ui/markdown.post.ts:
// /api and /api/* are proxied straight to the Forge status service
// (server/middleware/forge-proxy.ts + server/utils/proxy.ts's proxyPaths()),
// and the `_ui` prefix stays clear of that proxy. This route does its own
// I/O (reading the resolved document) and its own caching; server/utils/help.ts
// stays pure (renderHelp is markdown in, {html, toc} out).
import { accessSync, constants as fsConstants, readFileSync } from 'node:fs'
import { helpCandidates, renderHelp, resolveHelpDocPath, type HelpDoc } from '../../utils/help'

const CACHE_TTL_MS = 60_000

// Keyed by the *resolved* document path (not just a timestamp): resolution
// itself is cheap (a handful of fs.access checks), so it always runs, and
// only the read+render is skipped when the winning candidate hasn't changed.
// That way a changed NITRO_HELP_DOC/FORGE_ROOT — or the installation copy at
// $FORGE_ROOT/docs/onboarding.md appearing or disappearing — is honoured on
// the very next request instead of waiting out a stale 60s window.
let cache: { path: string; at: number; doc: HelpDoc } | null = null

function isReadable(candidate: string): boolean {
  try {
    accessSync(candidate, fsConstants.R_OK)
    return true
  } catch {
    return false
  }
}

export default defineEventHandler((event) => {
  // The document is a declared runtime config key; `NITRO_HELP_DOC` is only
  // the env override Nitro maps onto it, so reading the config keeps the
  // default in one place and honours a value set any other way. Which of the
  // three candidates that produces is helpCandidates()' business.
  const docPath = resolveHelpDocPath(helpCandidates({
    helpDoc: useRuntimeConfig(event).helpDoc,
    forgeRoot: process.env.FORGE_ROOT,
    cwd: process.cwd(),
  }), isReadable)
  if (!docPath) {
    setResponseStatus(event, 404)
    return { html: '', toc: [] } satisfies HelpDoc
  }

  const now = Date.now()
  if (cache && cache.path === docPath && now - cache.at < CACHE_TTL_MS) return cache.doc

  const source = readFileSync(docPath, 'utf8')
  const doc = renderHelp(source)
  cache = { path: docPath, at: now, doc }
  return doc
})

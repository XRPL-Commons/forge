// Renders an issue body to sanitized HTML for the issue detail route.
// It lives under /_ui/ rather than /api/ because /api and /api/* are proxied
// straight to the Forge status service (server/utils/proxy.ts), and this is
// the UI's own endpoint. It reads nothing and writes nothing: the body it is
// given is the body the client already received from GET /api/issues.
import { renderMarkdown } from '../../utils/markdown'

const MAX_BODY_BYTES = 64 * 1024
// "owner/name" as GitHub spells it; anything else cannot be turned into a
// repository URL, so links stay relative and are dropped by the sanitizer.
const REPO_NAME = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/

export default defineEventHandler(async (event) => {
  const raw = await readRawBody(event, 'utf8')
  if (raw && Buffer.byteLength(raw, 'utf8') > MAX_BODY_BYTES) {
    setResponseStatus(event, 413)
    return { html: '' }
  }
  let payload: unknown = null
  try { payload = raw ? JSON.parse(raw) : null } catch { payload = null }
  const body = (payload ?? {}) as { text?: unknown; repo?: unknown }
  const text = typeof body.text === 'string' ? body.text : ''
  const repo = typeof body.repo === 'string' && REPO_NAME.test(body.repo) ? body.repo : ''
  return { html: renderMarkdown(text, repo) }
})

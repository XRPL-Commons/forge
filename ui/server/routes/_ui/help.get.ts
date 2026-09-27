// Bounded, allowlisted guide reads. Setup retains its existing override chain.
import { accessSync, constants as fsConstants, readFileSync } from 'node:fs'
import { helpTopic } from '../../../shared/help-topics'
import { helpCandidates, renderHelp, resolveHelpDocPath, type HelpDoc } from '../../utils/help'

const cache = new Map<string, { at: number; doc: HelpDoc }>()
function isReadable(candidate: string): boolean {
  try { accessSync(candidate, fsConstants.R_OK); return true } catch { return false }
}

export default defineEventHandler((event) => {
  const topic = helpTopic(getQuery(event).topic)
  if (!topic) throw createError({ statusCode: 404, statusMessage: 'Help topic not found' })
  const docPath = resolveHelpDocPath(helpCandidates({
    helpDoc: useRuntimeConfig(event).helpDoc,
    forgeRoot: process.env.FORGE_ROOT,
    cwd: process.cwd(),
    topic: topic.id,
  }), isReadable)
  if (!docPath) throw createError({ statusCode: 404, statusMessage: 'Help guide unavailable' })
  const now = Date.now()
  const hit = cache.get(docPath)
  if (hit && now - hit.at < 60_000) return hit.doc
  let source: string
  try { source = readFileSync(docPath, 'utf8') }
  catch { throw createError({ statusCode: 503, statusMessage: 'Help guide unavailable' }) }
  const doc = renderHelp(source)
  if (cache.size >= 12) cache.clear()
  cache.set(docPath, { at: now, doc })
  return doc
})

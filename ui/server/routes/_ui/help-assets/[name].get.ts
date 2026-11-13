import { readFileSync } from 'node:fs'
import path from 'node:path'
import { helpAsset } from '../../../../shared/help-assets'

export default defineEventHandler((event) => {
  const asset = helpAsset(getRouterParam(event, 'name'))
  if (!asset) throw createError({ statusCode: 404, statusMessage: 'Help image not found' })
  // Installed UI releases own their docs directory. Development runs from ui/.
  const root = process.env.FORGE_ROOT || path.resolve(process.cwd(), '..')
  let body: Buffer
  try { body = readFileSync(path.join(root, 'docs/assets', asset.name)) }
  catch { throw createError({ statusCode: 404, statusMessage: 'Help image unavailable' }) }
  setHeader(event, 'Content-Type', 'image/svg+xml; charset=utf-8')
  setHeader(event, 'X-Content-Type-Options', 'nosniff')
  setHeader(event, 'Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; sandbox; frame-ancestors 'none'")
  // Release names change on asset edits; do not retain a previous release forever.
  setHeader(event, 'Cache-Control', 'private, max-age=300')
  return body
})

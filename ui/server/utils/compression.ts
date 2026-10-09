import { gzip } from 'node:zlib'
import { promisify } from 'node:util'

const compress = promisify(gzip)

export function acceptsGzip(value = ''): boolean {
  const encodings = new Map(value.toLowerCase().split(',').map(part => {
    const [name, ...parameters] = part.trim().split(';')
    const q = parameters.find(parameter => parameter.trim().startsWith('q='))?.trim().slice(2)
    return [name?.trim(), q === undefined ? 1 : Number(q)] as const
  }))
  const quality = encodings.get('gzip') ?? encodings.get('*') ?? 0
  return Number.isFinite(quality) && quality > 0 && quality <= 1
}

export async function compressJson(body: Buffer, acceptEncoding: string | undefined, contentType: string | null, status: number): Promise<Buffer | null> {
  if (status !== 200 || body.length < 1024 || !contentType?.startsWith('application/json') || !acceptsGzip(acceptEncoding)) return null
  const result = await compress(body)
  return result.length < body.length ? result : null
}

const views: Record<string, string> = { '/api/factory': 'factory', '/api/issues': 'issues', '/api/delivery': 'delivery' }
const metrics = new Set(['request', 'projection', 'cache_miss', 'cache_hit', 'cache_validated', 'conditional'])

// Only numeric timing fields and fixed view names leave this collector. Never
// record request URLs, query values, payloads or authentication headers.
export function createHttpTimingRecorder(emit: (sample: object) => void) {
  let count = 0
  return (path: string, status: number, header: string | null, browserMs: number) => {
    const [pathname, query] = path.split('?')
    let view = views[pathname!]
    if (view === 'factory') {
      const section = new URLSearchParams(query).get('section')
      if (section === 'board' || section === 'completed') view = section
    }
    if (!view || !header || count >= 100) return
    const serverMs: Record<string, number> = {}
    for (const field of header.split(',')) {
      const match = /^\s*([a-z_]+);dur=(\d+(?:\.\d+)?)\s*$/.exec(field)
      if (match && metrics.has(match[1]!) && Number.isFinite(Number(match[2]))) serverMs[match[1]!] = Number(match[2])
    }
    if (serverMs.request === undefined) return
    count++
    emit({ view, status, browserMs: Math.round(browserMs * 1000) / 1000, serverMs })
  }
}

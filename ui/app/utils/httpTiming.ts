const views: Record<string, string> = { '/api/factory': 'factory', '/api/issues': 'issues', '/api/delivery': 'delivery' }
const metrics = new Set(['request', 'projection', 'cache_miss', 'cache_hit', 'cache_validated', 'conditional'])

export interface HttpTimingSample {
  view: string
  source: 'server_render' | 'browser'
  status: number
  fetchMs: number
  serverMs: Record<string, number>
}

// Only numeric timing fields and fixed view names leave this collector. Never
// record request URLs, query values, payloads or authentication headers.
export function createHttpTimingRecorder(samples: HttpTimingSample[], source: HttpTimingSample['source']) {
  return (path: string, status: number, header: string | null, fetchMs: number) => {
    const [pathname, query] = path.split('?')
    let view = views[pathname!]
    const parameters = new URLSearchParams(query)
    if (view === 'factory') {
      const section = parameters.get('section')
      if (section === 'board' || section === 'completed') view = section
    }
    if (view === 'issues') view = parameters.has('issue') ? 'issue_detail' : 'issue_list'
    if (!view || !header || samples.length >= 100 || !Number.isFinite(fetchMs) || fetchMs < 0) return
    const serverMs: Record<string, number> = {}
    for (const field of header.split(',')) {
      const match = /^\s*([a-z_]+);dur=(\d+(?:\.\d+)?)\s*$/.exec(field)
      if (match && metrics.has(match[1]!) && Number.isFinite(Number(match[2]))) serverMs[match[1]!] = Number(match[2])
    }
    if (serverMs.request === undefined) return
    samples.push({ view, source, status, fetchMs: Math.round(fetchMs * 1000) / 1000, serverMs })
  }
}

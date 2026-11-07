// Imported only by HelpDiagram after mount. Other pages never fetch Mermaid.
let renderer: Promise<typeof import('mermaid')['default']> | undefined
export function helpDiagramRenderer() {
  renderer ??= import('mermaid').then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', theme: 'dark', layout: 'dagre', look: 'classic',
      suppressErrorRendering: true, maxTextSize: 50_000, maxEdges: 200,
      htmlLabels: false,
      secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges', 'suppressErrorRendering', 'htmlLabels'],
    })
    return mermaid
  }).catch(error => { renderer = undefined; throw error })
  return renderer
}

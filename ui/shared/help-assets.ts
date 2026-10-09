// Installation-owned illustrations. Both the renderer and asset route use this
// exact catalog; document text can never select a path outside it.
export const HELP_ASSETS = {
  'setup-hero.svg': { width: 600, height: 340 },
  'workflow-hero.svg': { width: 600, height: 330 },
  'discussion-hero.svg': { width: 600, height: 350 },
  'runtime-hero.svg': { width: 600, height: 350 },
  'factory-bays.svg': { width: 600, height: 560 },
  'personas.svg': { width: 600, height: 550 },
  'settings-screen.svg': { width: 600, height: 590 },
  'issues-screen.svg': { width: 600, height: 490 },
  'workbench-screen.svg': { width: 600, height: 510 },
  'discussion-screen.svg': { width: 600, height: 540 },
} as const

export function helpAsset(name: unknown) {
  if (typeof name !== 'string' || !Object.hasOwn(HELP_ASSETS, name)) return null
  return { name, ...HELP_ASSETS[name as keyof typeof HELP_ASSETS] }
}

export function helpImage(source: string) {
  const match = /^(?:assets\/|\/_ui\/help-assets\/)([a-z-]+\.svg)$/.exec(source)
  return match ? helpAsset(match[1]) : null
}

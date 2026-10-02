import { readFileSync, readdirSync } from 'node:fs'
import { describe, it, expect } from 'vitest'
describe('theme', () => {
  it('declares the Forge tokens', () => {
    // Indirect through variables: Vite statically rewrites the literal
    // `new URL('...', import.meta.url)` pattern into a dev-server asset URL
    // under the happy-dom test environment, which breaks readFileSync.
    const cssPath = '../../app/assets/css/main.css'
    const base = import.meta.url
    const css = readFileSync(new URL(cssPath, base), 'utf8')
    for (const hex of ['#101416', '#192024', '#303b40', '#bfe6cd', '#abcfff', '#ecc486']) expect(css).toContain(hex)
  })

  it('maps --ui-primary to the mint-200 token', () => {
    const cssPath = '../../app/assets/css/main.css'
    const base = import.meta.url
    const css = readFileSync(new URL(cssPath, base), 'utf8')
    expect(css).toContain('--ui-primary: var(--color-mint-200)')
  })

  it('keeps role colours in main.css only', () => {
    const base = import.meta.url
    const css = readFileSync(new URL('../../app/assets/css/main.css', base), 'utf8')
    const roles = ['#94bfff', '#c9a4f4', '#80d7bd', '#edbd77', '#c0cbcd']
    for (const role of ['build', 'review', 'audit', 'merge', 'controller', 'recovery']) expect(css).toContain(`--color-role-${role}:`)
    for (const dir of ['../../app/components/', '../../app/pages/', '../../app/pages/settings/', '../../app/pages/issues/']) {
      const url = new URL(dir, base)
      for (const name of readdirSync(url).filter(file => file.endsWith('.vue'))) {
        const source = readFileSync(new URL(name, url), 'utf8').toLowerCase()
        for (const hex of roles) expect(source, `${dir}${name}`).not.toContain(hex)
      }
    }
  })
})

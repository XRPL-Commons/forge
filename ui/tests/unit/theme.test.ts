import { readFileSync } from 'node:fs'
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
})

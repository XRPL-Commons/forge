// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { renderHelp, resolveHelpDocPath } from '../../server/utils/help'

const ONBOARDING_MD = fileURLToPath(new URL('../../../docs/onboarding.md', import.meta.url))

describe('renderHelp', () => {
  it('gives every h1/h2/h3 heading a slugified id', () => {
    const { html } = renderHelp('# Set up Forge\n\n## GitHub repository protection\n')
    expect(html).toContain('<h1 id="set-up-forge">Set up Forge</h1>')
    expect(html).toContain('<h2 id="github-repository-protection">GitHub repository protection</h2>')
  })

  it('collects only h1/h2 headings into the toc, in document order', () => {
    const { toc } = renderHelp('# One\n\n## Two\n\n### Three\n\n## Four\n')
    expect(toc).toEqual([
      { id: 'one', text: 'One', level: 1 },
      { id: 'two', text: 'Two', level: 2 },
      { id: 'four', text: 'Four', level: 2 },
    ])
  })

  it('renders paragraphs, bullet lists and numbered lists', () => {
    const { html } = renderHelp('A paragraph.\n\n- First\n- Second\n\n1. Step one\n2. Step two\n')
    expect(html).toContain('<p>A paragraph.</p>')
    expect(html).toContain('<ul><li>First</li><li>Second</li></ul>')
    expect(html).toContain('<ol><li>Step one</li><li>Step two</li></ol>')
  })

  it('renders a 4-space-indented code block and inline code', () => {
    const { html } = renderHelp('Run `bin/forge setup` then:\n\n    forge service --root /srv/forge\n')
    expect(html).toContain('<code>bin/forge setup</code>')
    expect(html).toContain('<pre><code>forge service --root /srv/forge</code></pre>')
  })

  it('rewrites a relative *.md link to the GitHub blob URL', () => {
    const { html } = renderHelp('See [installation](installation.md) for details.\n')
    expect(html).toContain('href="https://github.com/XRPL-Commons/forge/blob/main/docs/installation.md"')
    expect(html).toContain('>installation</a>')
  })

  it('leaves an absolute http(s) link alone', () => {
    const { html } = renderHelp('[GitHub docs](https://docs.github.com/en/rulesets)\n')
    expect(html).toContain('href="https://docs.github.com/en/rulesets"')
  })

  it('renders a javascript: link as plain text', () => {
    const { html } = renderHelp('[Click me](javascript:alert(1))\n')
    expect(html).not.toContain('<a')
    expect(html).not.toContain('href=')
    expect(html).toContain('Click me')
  })

  it('renders a relative link that is not a bare *.md filename as plain text', () => {
    const { html } = renderHelp('[Settings](/settings)\n')
    expect(html).not.toContain('href=')
    expect(html).toContain('Settings')
  })

  it('strips an embedded <img onerror> to inert text, never emitting the tag', () => {
    const { html } = renderHelp('Before <img src=x onerror="window.pwned=1"> after\n')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('onerror=1')
    expect(html).toContain('&lt;img')
    expect(html).toContain('Before')
    expect(html).toContain('after')
  })

  it('never emits a script tag however the doc smuggles one in', () => {
    const { html } = renderHelp('Before\n\n<script>window.injected=1</script>\n\nAfter\n')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('window.injected=1</script>')
  })

  it('the real docs/onboarding.md still produces the two anchors the UI deep-links', () => {
    const source = readFileSync(ONBOARDING_MD, 'utf8')
    const { html, toc } = renderHelp(source)
    expect(html).toContain('id="host-installation"')
    expect(html).toContain('id="github-repository-protection"')
    expect(toc.map((entry) => entry.id)).toContain('github-repository-protection')
    expect(toc.map((entry) => entry.id)).toContain('host-installation')
  })
})

describe('resolveHelpDocPath', () => {
  const repoDefault = '/repo/docs/onboarding.md'

  it('prefers NITRO_HELP_DOC when it is readable, even with FORGE_ROOT set', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: '/custom/help.md', forgeRoot: '/srv/forge', repoDefault },
      (path) => path === '/custom/help.md' || path === '/srv/forge/docs/onboarding.md' || path === repoDefault,
    )
    expect(resolved).toBe('/custom/help.md')
  })

  it('falls through an unreadable NITRO_HELP_DOC to FORGE_ROOT', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: '/custom/help.md', forgeRoot: '/srv/forge', repoDefault },
      (path) => path === '/srv/forge/docs/onboarding.md' || path === repoDefault,
    )
    expect(resolved).toBe('/srv/forge/docs/onboarding.md')
  })

  it('uses FORGE_ROOT over the repo default when NITRO_HELP_DOC is unset', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: null, forgeRoot: '/srv/forge', repoDefault },
      (path) => path === '/srv/forge/docs/onboarding.md' || path === repoDefault,
    )
    expect(resolved).toBe('/srv/forge/docs/onboarding.md')
  })

  it('falls through to the repo default when nothing else is readable', () => {
    const resolved = resolveHelpDocPath(
      { nitroHelpDoc: '/custom/help.md', forgeRoot: '/srv/forge', repoDefault },
      (path) => path === repoDefault,
    )
    expect(resolved).toBe(repoDefault)
  })

  it('returns null when no candidate is readable', () => {
    const resolved = resolveHelpDocPath({ nitroHelpDoc: null, forgeRoot: null, repoDefault }, () => false)
    expect(resolved).toBeNull()
  })
})

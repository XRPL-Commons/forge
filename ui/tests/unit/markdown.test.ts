import { describe, it, expect } from 'vitest'
import { renderMarkdown } from '../../server/utils/markdown'

const REPO = 'XRPL-Commons/academy'

describe('renderMarkdown', () => {
  it('never emits a script, however the issue body smuggles one in', () => {
    const html = renderMarkdown('Before\n\n<script>window.injected=1</script>\n\nAfter', REPO)
    expect(html).not.toContain('<script')
    expect(html).not.toContain('window.injected=1</script>')
    expect(html).toContain('Before')
    expect(html).toContain('After')
  })

  it('keeps a GitHub-flavoured table', () => {
    const html = renderMarkdown('| Role | Slot |\n| --- | --- |\n| build | 1 |', REPO)
    expect(html).toContain('<table>')
    expect(html).toContain('<th>Role</th>')
    expect(html).toContain('<td>build</td>')
  })

  it('resolves a repository-relative link to the GitHub blob URL, opened in a new tab', () => {
    const html = renderMarkdown('See [the navigation spec](docs/x.md) for details', REPO)
    expect(html).toContain(`href="https://github.com/${REPO}/blob/HEAD/docs/x.md"`)
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('leaves an absolute http link alone', () => {
    const html = renderMarkdown('[XRPL](https://xrpl.org/docs)', REPO)
    expect(html).toContain('href="https://xrpl.org/docs"')
  })

  it('drops a javascript: link to plain text', () => {
    const html = renderMarkdown('[Click me](javascript:alert(1))', REPO)
    expect(html).not.toContain('<a')
    expect(html).not.toContain('href=')
    expect(html).toContain('Click me')
  })

  it('drops a link whose scheme is not http, https or mailto', () => {
    const html = renderMarkdown('[Fetch it](ftp://example.com/payload)', REPO)
    expect(html).not.toContain('href=')
    expect(html).not.toContain('ftp://')
    expect(html).toContain('Fetch it')
  })

  it('keeps a mailto link', () => {
    const html = renderMarkdown('[Write](mailto:ops@example.com)', REPO)
    expect(html).toContain('href="mailto:ops@example.com"')
  })

  it('renders task list items as disabled checkboxes', () => {
    const html = renderMarkdown('- [x] Confirm mobile layout\n- [ ] Ship it', REPO)
    const inputs = html.match(/<input[^>]*>/g) ?? []
    expect(inputs).toHaveLength(2)
    expect(inputs[0]).toContain('type="checkbox"')
    expect(inputs[0]).toMatch(/disabled/)
    expect(inputs[0]).toMatch(/checked/)
    expect(inputs[1]).toMatch(/disabled/)
    expect(inputs[1]).not.toMatch(/checked/)
    expect(html).toContain('Confirm mobile layout')
    expect(html).not.toContain('[x]')
  })

  it('removes an input that is not a checkbox', () => {
    const html = renderMarkdown('<input type="text" name="password">', REPO)
    expect(html).not.toContain('<input')
  })

  it('caps a very long body at 16000 characters of source', () => {
    const html = renderMarkdown('x'.repeat(20000), REPO)
    expect(html.length).toBeLessThan(16100)
  })

  it('returns nothing for an empty body', () => {
    expect(renderMarkdown('', REPO)).toBe('')
    expect(renderMarkdown('   ', REPO)).toBe('')
  })
})

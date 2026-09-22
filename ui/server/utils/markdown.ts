// Issue descriptions are GitHub markdown written by anyone who can open an
// issue, so they are rendered on the server and sanitized before they ever
// reach the operator's browser: markdown-it parses with raw HTML disabled,
// sanitize-html then enforces the tag/attribute list of contracts §3.3.
// The rules match the legacy `legacy/markdown.js` (marked + DOMPurify) so the
// same issue body renders the same way in both UIs.
import MarkdownIt from 'markdown-it'
import type { MarkdownIt as MarkdownItInstance, StateCore, Token } from 'markdown-it'
import sanitizeHtml from 'sanitize-html'

// The API already truncates a body to 16000 characters (`body_truncated`
// flags it); this is the same ceiling applied again, so a body that reaches
// this endpoint by another route cannot make the renderer do unbounded work.
export const MAX_MARKDOWN_CHARS = 16000

const ALLOWED_TAGS = [
  'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 's', 'a',
  'code', 'pre', 'blockquote', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'details', 'summary', 'input',
]

const ALLOWED_SCHEMES = ['http:', 'https:', 'mailto:']

const TASK_ITEM = /^\[([ xX])\]\s+/

// markdown-it has no GFM task lists, and the contract's sanitizer allows a
// checkbox input precisely because the legacy renderer produced one. Rather
// than add a plugin dependency, the checkbox is injected as a token: an
// `html_inline` token is emitted by the renderer verbatim even though raw
// HTML is disabled for *parsing*, which is exactly the distinction we want —
// we author this markup, the issue body cannot.
function taskLists(md: MarkdownItInstance): void {
  md.core.ruler.push('forge_task_lists', (state: StateCore) => {
    const tokens = state.tokens
    for (let index = 2; index < tokens.length; index += 1) {
      const token = tokens[index]
      if (!token || token.type !== 'inline') continue
      if (tokens[index - 1]?.type !== 'paragraph_open' || tokens[index - 2]?.type !== 'list_item_open') continue
      const match = TASK_ITEM.exec(token.content)
      if (!match) continue
      token.content = token.content.slice(match[0].length)
      const first = token.children?.[0]
      if (first && first.type === 'text') first.content = first.content.replace(TASK_ITEM, '')
      const box = new state.Token('html_inline', '', 0) as Token
      box.content = `<input type="checkbox" disabled${match[1] === ' ' ? '' : ' checked'}> `
      token.children?.unshift(box)
    }
  })
}

const md = new MarkdownIt({ html: false, linkify: true, typographer: false, breaks: false }).use(taskLists)

// A link is resolved the way GitHub resolves it inside an issue body, so
// `docs/x.md` points at the file in the repository rather than at the
// operator UI. Anything that is not http(s) or mailto loses its href and
// stays as text — the legacy DOMPurify config did the same.
function resolveHref(href: string, repo: string): string | null {
  if (!href) return null
  try {
    const url = new URL(href, `https://github.com/${repo}/blob/HEAD/`)
    return ALLOWED_SCHEMES.includes(url.protocol) ? url.href : null
  } catch {
    return null
  }
}

export function renderMarkdown(text: string, repo: string): string {
  const source = (text ?? '').slice(0, MAX_MARKDOWN_CHARS)
  if (!source.trim()) return ''
  const rendered = md.render(source)
  return sanitizeHtml(rendered, {
    allowedTags: ALLOWED_TAGS,
    allowedAttributes: {
      a: ['href', 'target', 'rel', 'title'],
      ol: ['start'],
      th: ['align'],
      td: ['align'],
      input: ['type', 'checked', 'disabled'],
      details: ['open'],
    },
    allowedSchemes: ['http', 'https', 'mailto'],
    disallowedTagsMode: 'discard',
    transformTags: {
      a: (tagName, attribs) => {
        const href = resolveHref(attribs.href ?? '', repo)
        if (!href) return { tagName, attribs: {} }
        return {
          tagName,
          attribs: { href, target: '_blank', rel: 'noopener noreferrer', ...(attribs.title ? { title: attribs.title } : {}) },
        }
      },
      input: (tagName, attribs) => (attribs.type === 'checkbox'
        ? { tagName, attribs: { type: 'checkbox', disabled: 'disabled', ...('checked' in attribs ? { checked: 'checked' } : {}) } }
        : { tagName, attribs: {} }),
    },
    // transformTags cannot delete an element, so a non-checkbox input — the
    // shape a credential-phishing issue body would use — is dropped here.
    exclusiveFilter: frame => frame.tag === 'input' && frame.attribs.type !== 'checkbox',
  }).trim()
}

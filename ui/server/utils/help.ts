// Renders the installation-owned setup guide (docs/onboarding.md, or an
// installation copy at $FORGE_ROOT/docs/onboarding.md) for GET /_ui/help.
//
// This is a *restricted* Markdown subset (contracts §3.9), not a general
// renderer: it is a faithful TypeScript port of the reference implementation
// `forge/setup_help.py` (`inline`/`render`), which the contract names as the
// source of truth. Every character of source text is escaped as it is
// emitted; the only tags this module ever writes are the literal `<h1-3>`,
// `<p>`, `<ul>`/`<ol>`/`<li>`, `<code>`, `<pre>` and `<a>` strings below, so
// nothing in the document — however it tries to smuggle a tag in — can ever
// become live markup. `sanitizeHtml` is then run over the result anyway, as
// the same defence-in-depth belt Task 7's `renderMarkdown` uses: the tag and
// attribute allowlist is imported from `server/utils/markdown.ts`
// (`MARKDOWN_ALLOWLIST`) rather than redeclared here, minus `input` (a help
// doc never has checkboxes), plus `id` on the heading tags so the anchors
// this page's own sidebar and other pages' deep links rely on survive.
import sanitizeHtml from 'sanitize-html'
import { MARKDOWN_ALLOWLIST } from './markdown'

export type TocEntry = { id: string; text: string; level: 1 | 2 }
export type HelpDoc = { html: string; toc: TocEntry[] }

// Task 7's allowlist minus `input` (renderHelp never emits a checkbox), plus
// `id` on the heading tags — the one addition beyond Task 7's list, since an
// issue body never needs a heading anchor but this page's sidebar and other
// pages' deep links (#host-installation, #github-repository-protection) do.
const ALLOWED_TAGS = MARKDOWN_ALLOWLIST.tags.filter((tag) => tag !== 'input')
const ALLOWED_ATTRIBUTES: Record<string, string[]> = {
  ...Object.fromEntries(Object.entries(MARKDOWN_ALLOWLIST.attributes).filter(([tag]) => tag !== 'input')),
  h1: ['id'], h2: ['id'], h3: ['id'],
}

// Only a bare "name.md" — letters and hyphens, no path separators, matching
// `forge/setup_help.py`'s `re.fullmatch(r'[a-z-]+\.md', url)` — is treated as
// a sibling of docs/onboarding.md and rewritten to the GitHub blob URL. Any
// other relative link (a path, a fragment, an extensionless slug) is not a
// document this help page can resolve, so it renders as text instead of a
// dead or surprising href.
const REPO_MD_FILE = /^[a-z-]+\.md$/
const DOCS_BLOB_BASE = 'https://github.com/XRPL-Commons/forge/blob/main/docs/'

const HEADING = /^(#{1,3}) (.+)$/
const LIST_ITEM = /^(?:(-)|\d+\.) (.+)$/
const INLINE_TOKEN = /`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)/g

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// GitHub's own slugifier, as ported by setup_help.py: lowercase, collapse
// every run of non-alphanumeric characters to one hyphen, trim the ends.
function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
}

// Resolves a link's target, or returns null when it must render as plain
// text: only http(s) links survive, and only after a bare *.md filename has
// had the chance to become one.
function resolveHref(url: string): string | null {
  const rewritten = REPO_MD_FILE.test(url) ? DOCS_BLOB_BASE + url : url
  let parsed: URL
  try {
    parsed = new URL(rewritten)
  } catch {
    return null
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
}

// Inline spans: backtick code and [label](url) links, in source order;
// everything else is literal text. Matches setup_help.py's `inline()`.
function renderInline(text: string): string {
  let out = ''
  let lastIndex = 0
  for (const match of text.matchAll(INLINE_TOKEN)) {
    out += escapeHtml(text.slice(lastIndex, match.index))
    if (match[1] !== undefined) {
      out += `<code>${escapeHtml(match[1])}</code>`
    } else {
      const label = match[2] ?? ''
      const href = resolveHref(match[3] ?? '')
      out += href ? `<a href="${escapeHtml(href)}">${escapeHtml(label)}</a>` : escapeHtml(label)
    }
    lastIndex = match.index + match[0].length
  }
  out += escapeHtml(text.slice(lastIndex))
  return out
}

// Pure: markdown source in, sanitized HTML + a table of contents out. No
// file I/O and no caching — those belong to the route (server/routes/_ui/help.get.ts).
export function renderHelp(markdown: string): HelpDoc {
  const blocks: string[] = []
  const toc: TocEntry[] = []
  let paragraph: string[] = []
  let items: string[] = []
  let listType: 'ul' | 'ol' | null = null
  let code: string[] = []

  function flush(): void {
    if (paragraph.length) {
      blocks.push(`<p>${renderInline(paragraph.join(' '))}</p>`)
      paragraph = []
    }
    if (items.length) {
      const tag = listType ?? 'ul'
      blocks.push(`<${tag}>${items.map((item) => `<li>${renderInline(item)}</li>`).join('')}</${tag}>`)
      items = []
      listType = null
    }
    if (code.length) {
      blocks.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
      code = []
    }
  }

  for (const line of markdown.split('\n')) {
    const headingMatch = HEADING.exec(line)
    const itemMatch = LIST_ITEM.exec(line)
    if (!line.trim()) {
      flush()
    } else if (headingMatch) {
      flush()
      const level = headingMatch[1].length
      const text = headingMatch[2]
      const id = slugify(text)
      blocks.push(`<h${level} id="${id}">${renderInline(text)}</h${level}>`)
      if (level <= 2) toc.push({ id, text, level: level as 1 | 2 })
    } else if (itemMatch) {
      const kind: 'ul' | 'ol' = itemMatch[1] ? 'ul' : 'ol'
      if (paragraph.length || code.length || (listType && listType !== kind)) flush()
      listType = kind
      items.push(itemMatch[2])
    } else if (items.length && line.startsWith(' ')) {
      // A wrapped continuation line of the current list item.
      items[items.length - 1] += ` ${line.trim()}`
    } else if (line.startsWith('    ')) {
      if (paragraph.length || items.length) flush()
      code.push(line.slice(4))
    } else {
      if (items.length || code.length) flush()
      paragraph.push(line.trim())
    }
  }
  flush()

  const html = sanitizeHtml(blocks.join(''), {
    allowedTags: ALLOWED_TAGS,
    allowedAttributes: ALLOWED_ATTRIBUTES,
    allowedSchemes: ['http', 'https'],
    disallowedTagsMode: 'discard',
  })

  return { html, toc }
}

// --- Document resolution (pure; the route supplies the real inputs and I/O) ---

export type HelpDocCandidates = {
  nitroHelpDoc: string | null
  forgeRoot: string | null
  repoDefault: string
}

// Precedence (ruled): NITRO_HELP_DOC, else $FORGE_ROOT/docs/onboarding.md,
// else the repo's own ../docs/onboarding.md — the first *readable* one wins,
// because the installation copy at $FORGE_ROOT overrides the repo file only
// when it actually exists. `isReadable` is injected so this stays pure and
// unit-testable without touching a filesystem; the route passes a real check.
export function resolveHelpDocPath(candidates: HelpDocCandidates, isReadable: (path: string) => boolean): string | null {
  const ordered = [
    candidates.nitroHelpDoc,
    candidates.forgeRoot ? `${candidates.forgeRoot.replace(/\/+$/, '')}/docs/onboarding.md` : null,
    candidates.repoDefault,
  ].filter((path): path is string => Boolean(path))
  return ordered.find((path) => isReadable(path)) ?? null
}

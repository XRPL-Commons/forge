"""Render the installation-owned setup guide without allowing embedded HTML."""
import html
import re
from urllib.parse import urlparse


def inline(text):
    def token(match):
        if match.group(1) is not None:
            return '<code>' + html.escape(match.group(1)) + '</code>'
        label, url = match.group(2), match.group(3)
        if re.fullmatch(r'[a-z-]+\.md', url):
            url = 'https://github.com/XRPL-Commons/forge/blob/main/docs/' + url
        if urlparse(url).scheme not in ('http', 'https'):
            return html.escape(label)
        return '<a href="' + html.escape(url, quote=True) + '">' + html.escape(label) + '</a>'
    pattern = re.compile(r'`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)')
    parts, end = [], 0
    for match in pattern.finditer(text):
        parts.extend((html.escape(text[end:match.start()]), token(match)))
        end = match.end()
    return ''.join(parts) + html.escape(text[end:])


def render(guide):
    blocks, paragraph, items, code = [], [], [], []
    list_type = None

    def flush():
        nonlocal list_type
        if paragraph:
            blocks.append('<p>' + inline(' '.join(paragraph)) + '</p>'); paragraph.clear()
        if items:
            blocks.append('<' + list_type + '>' + ''.join('<li>' + inline(item) + '</li>' for item in items) + '</' + list_type + '>')
            items.clear(); list_type = None
        if code:
            blocks.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>'); code.clear()

    for line in guide.splitlines():
        heading = re.match(r'^(#{1,3}) (.+)$', line)
        item = re.match(r'^(?:([-])|\d+\.) (.+)$', line)
        if not line.strip():
            flush()
        elif heading:
            flush(); level = len(heading[1]); anchor = re.sub(r'[^a-z0-9]+', '-', heading[2].lower()).strip('-')
            blocks.append(f'<h{level} id="{anchor}">' + inline(heading[2]) + f'</h{level}>')
        elif item:
            kind = 'ul' if item[1] else 'ol'
            if paragraph or code or (list_type and list_type != kind): flush()
            list_type = kind; items.append(item[2])
        elif items and line.startswith(' '):
            items[-1] += ' ' + line.strip()
        elif line.startswith('    '):
            if paragraph or items: flush()
            code.append(line[4:])
        else:
            if items or code: flush()
            paragraph.append(line.strip())
    flush()
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Forge · Setup help</title><style>body{margin:0;background:#101416;color:#edf1ee;font:16px/1.65 system-ui}'
            '.help-content{max-width:760px;margin:24px auto;padding:0 24px 48px}a{color:#abcfff}h1{font-size:32px}h2{margin-top:36px;font-size:22px}li{margin:10px 0}code{font-size:.9em;background:#192024;padding:2px 5px;border-radius:4px}'
            'pre{padding:16px;background:#192024;border:1px solid #303b40;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}p,li{overflow-wrap:anywhere}'
            'nav{margin:24px 0;padding:16px;background:#192024;border-radius:8px}nav a{display:block}a:focus-visible{outline:2px solid #abcfff;outline-offset:3px}</style></head><body><!--FORGE_NAVIGATION--><div class="help-content">'
            '<a href="/onboarding">← Back to setup</a><nav aria-label="Help topics"><a href="#github-repository-protection">Configure repository protection</a>'
            '<a href="#maintain-github-access">Manage GitHub access</a><a href="#host-installation">Coding accounts and VM setup</a></nav><main>' + ''.join(blocks) + '</main></div></body></html>')

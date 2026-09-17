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
    blocks, paragraph, items, code, topics = [], [], [], [], []
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
            if level <= 2:
                topics.append('<a href="#' + anchor + '">' + html.escape(heading[2]) + '</a>')
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
            '.help-layout{box-sizing:border-box;max-width:1200px;margin:32px auto;padding:0 32px 48px;display:grid;grid-template-columns:240px minmax(0,760px);gap:64px;align-items:start}'
            '.help-sidebar{position:sticky;top:calc(var(--forge-header-height,84px) + 24px);max-height:calc(100dvh - var(--forge-header-height,84px) - 48px);overflow:auto;font-size:14px}'
            '.help-sidebar>p{color:#acb7b9;font-size:12px;font-weight:650;letter-spacing:1px;text-transform:uppercase;margin:0 12px 16px}'
            '.help-sidebar>a{display:inline-block;margin:0 12px}.help-sidebar nav{display:grid;gap:4px}'
            '.help-sidebar nav a{display:block;padding:9px 12px;border-radius:6px;color:#acb7b9;text-decoration:none;overflow-wrap:anywhere}'
            '.help-sidebar nav a:hover,.help-sidebar nav a:focus-visible{background:#192024;color:#edf1ee}'
            '.help-layout main{min-width:0}.help-layout main>h1{font-size:32px;font-weight:650;line-height:1.2;letter-spacing:-.7px;margin:0 0 28px}'
            'a{color:#abcfff}h1{font-size:32px}h2{margin-top:36px;font-size:22px}li{margin:10px 0}code{font-size:.9em;background:#192024;padding:2px 5px;border-radius:4px}'
            'pre{padding:16px;background:#192024;border:1px solid #303b40;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}p,li{overflow-wrap:anywhere}'
            'a:focus-visible{outline:2px solid #abcfff;outline-offset:3px}'
            '@media(max-width:800px){.help-layout{grid-template-columns:minmax(0,1fr);gap:28px;margin-top:24px;padding:0 18px 36px}.help-sidebar{position:static;max-height:none}.help-sidebar nav{grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}.help-sidebar>p{margin-top:18px}h1{font-size:29px}}'
            '</style></head><body><!--FORGE_NAVIGATION--><div class="help-layout"><aside class="help-sidebar">'
            '<p>On this page</p><nav aria-label="Help topics">' + ''.join(topics) +
            '</nav></aside><main><p class="settings-eyebrow">Help</p>' + ''.join(blocks) + '</main></div></body></html>')

"""Shared, server-rendered navigation for every Forge operator page."""

FAVICON = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="12" fill="#101416"/>
<g transform="rotate(-6 32 32)" fill="none" stroke="#bfe6cd" stroke-width="2.5">
<rect x="9" y="9" width="46" height="46" rx="8"/>
<path d="M23 18h23v6H30v8h13v6H30v10h-7z" fill="#bfe6cd" stroke="none"/>
</g></svg>'''

STYLE = """<link rel="icon" type="image/svg+xml" sizes="any" href="/favicon.svg?v=1"><style>
:root{--forge-header-height:84px;scroll-padding-top:calc(var(--forge-header-height) + 16px)}
.forge-topbar{position:sticky;top:0;z-index:3;background:#101416;border-bottom:1px solid #303b40}
.forge-header{box-sizing:border-box;max-width:1600px;margin:0 auto;padding:26px 28px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#edf1ee}
.forge-header .forge-brand{display:flex;align-items:center;gap:10px;color:#edf1ee;text-decoration:none;font-size:22px;line-height:1.3;letter-spacing:-.6px;font-weight:750;white-space:nowrap}
.forge-header .forge-mark{box-sizing:border-box;display:grid;place-items:center;width:28px;height:28px;border:1px solid #bfe6cd;border-radius:5px;color:#bfe6cd;font-size:17px;transform:rotate(-6deg)}
.forge-header .forge-navigation{display:flex;align-items:center;flex-wrap:wrap;justify-content:flex-end;gap:10px 24px;margin:0;padding:0;border:0;background:none;border-radius:0}
.forge-header .forge-navigation a{display:inline-block;color:#acb7b9;text-decoration:none;padding:5px 0;white-space:nowrap;font-size:14px;font-weight:500}
.forge-header .forge-navigation a:hover{color:#edf1ee;text-decoration:underline;text-underline-offset:5px}
.forge-header .forge-navigation a[aria-current=page]{color:#bfe6cd;text-decoration:underline;text-underline-offset:5px}
.forge-header a:focus-visible{outline:2px solid #abcfff;outline-offset:5px;border-radius:3px}
.forge-header .forge-header-end{display:flex;align-items:center;gap:24px;flex-wrap:wrap;justify-content:flex-end}
.forge-header #saved{font-size:12px;color:#acb7b9}.forge-header #saved:empty{display:none}
@media(max-width:700px){.forge-header{padding:20px 18px;gap:16px}.forge-header .forge-header-end{width:100%;gap:8px;justify-content:flex-start}.forge-header .forge-navigation{justify-content:flex-start;gap:6px 20px}.forge-header .forge-navigation a{font-size:13px}.forge-header #saved{display:none}}
</style>"""


def render(page, path):
    """Fill the navigation slot without depending on client-side initialization."""
    current = '/' if path == '/factory' else path
    if path == '/settings/advanced':
        current = '/settings'
    links = []
    for href, label in (('/', 'Factory'), ('/settings/github', 'GitHub access'),
                        ('/settings/harnesses', 'Coding accounts'), ('/settings', 'Settings')):
        active = ' aria-current="page"' if current == href else ''
        links.append(f'<a href="{href}"{active}>{label}</a>')
    header = ('<div class="forge-topbar"><header class="forge-header"><a class="forge-brand" href="/" aria-label="Forge home">'
              '<span class="forge-mark" aria-hidden="true">F</span>Forge</a>'
              '<div class="forge-header-end"><span id="saved"></span>'
              '<nav class="forge-navigation" aria-label="Main navigation">' + ''.join(links) + '</nav></div></header></div>'
              '<script>(()=>{const bar=document.querySelector(".forge-topbar");'
              'const measure=()=>document.documentElement.style.setProperty("--forge-header-height",bar.getBoundingClientRect().height+"px");'
              'measure();new ResizeObserver(measure).observe(bar)})();</script>')
    return page.replace('</head>', STYLE + '</head>', 1).replace('<!--FORGE_NAVIGATION-->', header, 1)

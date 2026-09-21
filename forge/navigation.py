"""Shared, server-rendered navigation for every Forge operator page."""

from .illustrations import SPRITE
from .polling import SCRIPT as REFRESH_SCRIPT, STYLE as REFRESH_STYLE
from .select_menu import SCRIPT as SELECT_MENU_SCRIPT, STYLE as SELECT_MENU_STYLE

FAVICON = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="12" fill="#101416"/>
<g transform="rotate(-6 32 32)" fill="none" stroke="#bfe6cd" stroke-width="2.5">
<rect x="9" y="9" width="46" height="46" rx="8"/>
<path d="M23 18h23v6H30v8h13v6H30v10h-7z" fill="#bfe6cd" stroke="none"/>
</g></svg>'''

STYLE = """<link rel="icon" type="image/svg+xml" sizes="any" href="/favicon.svg?v=1"><style>
:root{--forge-header-height:84px;scroll-padding-top:calc(var(--forge-header-height) + 16px)}
.settings-eyebrow{font:650 12px/1.5 system-ui,sans-serif;text-transform:uppercase;letter-spacing:1.4px;color:#bfe6cd;margin:0 0 10px}
.role-illustration{flex:none}.role-illustration[data-role=build]{color:#94bfff}.role-illustration[data-role=review]{color:#c9a4f4}.role-illustration[data-role=audit]{color:#80d7bd}.role-illustration[data-role=controller]{color:#edbd77}
.forge-topbar{position:sticky;top:0;z-index:3;background:#101416;border-bottom:1px solid #303b40}
.forge-header{box-sizing:border-box;max-width:1600px;margin:0 auto;padding:16px 28px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#edf1ee}
.forge-header .forge-brand{display:flex;align-items:center;gap:10px;color:#edf1ee;text-decoration:none;font-size:22px;line-height:1.3;letter-spacing:-.6px;font-weight:750;white-space:nowrap}
.forge-header .forge-mark{box-sizing:border-box;display:grid;place-items:center;width:28px;height:28px;border:1px solid #bfe6cd;border-radius:5px;color:#bfe6cd;font-size:17px;transform:rotate(-6deg)}
.forge-header .forge-navigation{display:flex;align-items:center;flex-wrap:wrap;justify-content:flex-end;gap:8px 20px;margin:0;padding:0;border:0;background:none;border-radius:0}
.forge-header .forge-navigation a{display:inline-block;color:#acb7b9;text-decoration:none;padding:5px 0;white-space:nowrap;font-size:14px;font-weight:500}
.forge-header .forge-navigation a:hover{color:#edf1ee;text-decoration:underline;text-underline-offset:5px}
.forge-header .forge-navigation a[aria-current=page]{color:#bfe6cd;text-decoration:underline;text-underline-offset:5px}
.forge-header .forge-navigation .forge-help{display:inline-flex;align-items:center;justify-content:center;min-width:32px;min-height:32px;padding:0}.forge-help svg{display:block}
.forge-header a:focus-visible{outline:2px solid #abcfff;outline-offset:5px;border-radius:3px}
.forge-header .forge-header-end{display:flex;align-items:center;gap:18px;flex-wrap:wrap;justify-content:flex-end}
.forge-settings-navigation{box-sizing:border-box;max-width:1200px;margin:0 auto;padding:0 32px 10px;display:flex;gap:8px 24px;flex-wrap:wrap;font:14px/1.5 system-ui,sans-serif}.forge-settings-navigation a{display:inline-flex;align-items:center;min-height:36px;color:#acb7b9;text-decoration:none}.forge-settings-navigation a[aria-current=page]{color:#bfe6cd;text-decoration:underline;text-underline-offset:6px}.forge-settings-navigation a:focus-visible{outline:2px solid #abcfff;outline-offset:3px}
.forge-activity{display:inline-flex;align-items:center;gap:10px;font-size:12px;white-space:nowrap}.forge-activity[hidden]{display:none}
.forge-activity a{display:inline-flex;align-items:center;gap:5px;color:#acb7b9;text-decoration:none;min-height:32px}.forge-activity a[data-role=build]{color:#94bfff}.forge-activity a[data-role=review]{color:#c9a4f4}.forge-activity a[data-role=audit]{color:#80d7bd}.forge-activity a[data-role=merge]{color:#edbd77}
.forge-activity .worker-spinner{width:12px;height:12px;box-sizing:border-box;border:1.5px solid currentColor;border-right-color:transparent;border-radius:50%;animation:forge-working 1s linear infinite}
@keyframes forge-working{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.forge-activity .worker-spinner{animation:none}}
.forge-settings-page{background:#101416;color:#edf1ee;font:15px/1.55 system-ui,sans-serif}
.forge-settings-page>main,.forge-settings-page>.layout.solo{box-sizing:border-box;max-width:1200px;margin:0 auto;padding:32px 32px 48px}
.forge-settings-page h1{font-size:32px;font-weight:650;line-height:1.2;letter-spacing:-.7px;margin:0 0 16px;max-width:none}
.forge-settings-page .settings-eyebrow,.forge-settings-page .eyebrow{display:block;font-size:12px;line-height:1.5;font-weight:650;letter-spacing:1.4px;color:#bfe6cd;text-transform:uppercase;margin:0 0 10px}
.forge-settings-page #page-description,.forge-settings-page #screen-title+p{font-size:16px;line-height:1.55;margin:0;max-width:760px;color:#acb7b9}
.forge-settings-page section,.forge-settings-page .panel{background:#192024;border-color:#303b40;margin-top:28px;margin-bottom:28px}
.forge-settings-page .danger-zone{border-color:#66433f;margin-top:48px}
.forge-settings-page #setup-footer{display:none}
@media(max-width:700px){.forge-settings-page>main,.forge-settings-page>.layout.solo{padding:24px 18px 36px}.forge-settings-page h1{font-size:29px}.forge-settings-navigation{padding:0 18px 10px;gap:4px 18px}.forge-settings-navigation a{min-height:44px;font-size:13px}.forge-header{padding:12px 18px;gap:16px}.forge-activity a{min-height:44px}.forge-header .forge-header-end{flex:1;gap:8px;justify-content:flex-end}.forge-header .forge-navigation{justify-content:flex-end;gap:6px 20px}.forge-header .forge-navigation .forge-help{min-width:44px;min-height:44px}.forge-header .forge-navigation a{font-size:13px;min-height:44px;display:inline-flex;align-items:center}}
</style>"""


# Titles, breadcrumbs, menus and activity indicators keep their established form.
PILL_LINK = 'a[href^="/"]:not([href^="//"]):not(nav a):not(.forge-brand):not(.forge-activity a):not(.settings-eyebrow a):not(.eyebrow a):not(.issue-title):not(.repo-link):not([download])'
STYLE += '<style>' + PILL_LINK + ',.forge-nav-pill,.secondary-pill{display:inline-flex;align-items:center;justify-content:center;gap:6px;max-width:100%;box-sizing:border-box;min-height:32px;padding:5px 12px;border:1px solid #354147;border-radius:999px;background:transparent;color:#c0cbcd;font-size:13px;font-weight:500;line-height:1.5;text-decoration:none;white-space:normal;overflow-wrap:anywhere;text-align:center}' + PILL_LINK + ':hover,.forge-nav-pill:hover,.secondary-pill:hover{background:#20292e;border-color:#526269;color:#edf1ee}' + '@media(max-width:700px){' + PILL_LINK + ',.forge-nav-pill,.secondary-pill{min-height:44px}}' + '</style>'


def render(page, path):
    """Fill the navigation slot without depending on client-side initialization."""
    current = '/' if path == '/factory' else path
    if path == '/settings/repository':
        current = '/settings/github'
    if path == '/settings/advanced':
        current = '/settings'
    settings_page = path.startswith('/settings')
    if settings_page:
        page = page.replace('<body>', '<body class="forge-settings-page">', 1)
    active = ' aria-current="page"' if settings_page else ''
    links = [f'<a href="/settings"{active}>Settings</a>', '<a class="forge-help" href="/setup-help" aria-label="Help" title="Help"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3h.01"/></svg></a>']
    settings_nav = ''
    if path in ('/', '/issues', '/factory'):
        settings_nav = '<nav class="forge-settings-navigation" aria-label="Work navigation">' + ''.join(
            f'<a href="{href}"' + (' aria-current="page"' if (path in ('/', '/factory')) == (href == '/factory') else '') + f'>{label}</a>'
            for href,label in (('/factory','Activity'),('/issues','Issues'))) + '</nav>'
    if settings_page:
        tabs = []
        for href, label in (('/settings', 'General'), ('/settings/github', 'GitHub access'),
                            ('/settings/harnesses', 'Coding accounts')):
            active = ' aria-current="page"' if current == href else ''
            tabs.append(f'<a href="{href}"{active}>{label}</a>')
        settings_nav = '<nav class="forge-settings-navigation" aria-label="Settings navigation">' + ''.join(tabs) + '</nav>'
    header = ('<div class="forge-topbar"><header class="forge-header"><a class="forge-brand" href="/" aria-label="Forge home">'
              '<span class="forge-mark" aria-hidden="true">F</span>Forge</a>'
              '<div class="forge-header-end"><span id="forge-activity" class="forge-activity" role="status" aria-live="polite" hidden></span>'
              '<nav class="forge-navigation" aria-label="Main navigation">' + ''.join(links) + '</nav></div></header>' + settings_nav + '</div>'
              '<script>(()=>{const bar=document.querySelector(".forge-topbar");'
              'const measure=()=>document.documentElement.style.setProperty("--forge-header-height",bar.getBoundingClientRect().height+"px");'
              'measure();new ResizeObserver(measure).observe(bar)})();</script>')
    return page.replace('</head>', STYLE + REFRESH_STYLE + SELECT_MENU_STYLE + '</head>', 1).replace('<!--FORGE_NAVIGATION-->', SPRITE + header + REFRESH_SCRIPT + ACTIVITY_SCRIPT + SELECT_MENU_SCRIPT, 1)


ACTIVITY_SCRIPT = """<script>(()=>{
  const indicator=document.getElementById('forge-activity');
  let timer;
  function hide(){indicator.hidden=true;indicator.textContent='';indicator.removeAttribute('data-state');indicator.removeAttribute('title');}
  async function refresh(){
    if(document.hidden)return;
    try{
      const response=await fetch('/api/factory',{credentials:'same-origin',signal:AbortSignal.timeout(10000)});
      if(!response.ok)throw Error('Unavailable');
      const data=await response.json();if(!data.ok)throw Error('Unavailable');
      if(!data.active){hide();return;}
      const now=Date.now()/1000,maxAge=Math.max(120,(Number(data.runtime?.cycle_seconds)||30)*3);
      if(!data.observation?.at||now-data.observation.at>maxAge){hide();return;}
      const jobs=(data.jobs||[]).filter(job=>!job.cleanup&&!job.fenced&&!job.outcome);
      const working=jobs.filter(job=>job.phase==='running'&&(job.observed_at||job.last_observed)&&now-(job.observed_at||job.last_observed)<=maxAge);
      const roles={build:['builder','builders','stage-build'],review:['reviewer','reviewers','stage-review'],audit:['auditor','auditors','stage-audit'],merge:['merge worker','merge workers','runners']};
      const fragment=document.createDocumentFragment();
      for(const [role,[singular,plural,target]] of Object.entries(roles)){
        const count=working.filter(job=>job.role===role).length;if(!count)continue;
        const link=document.createElement('a');link.href='/factory#'+target;link.dataset.role=role;
        const label=`${count} ${count===1?singular:plural} working`;link.title=label;link.setAttribute('aria-label',label);
        const spinner=document.createElement('span');spinner.className='worker-spinner';spinner.setAttribute('aria-hidden','true');
        link.append(spinner,document.createTextNode(String(count)));fragment.append(link);
      }
      const queued=(data.issues||[]).filter(issue=>!issue.active&&['ready','built','reviewed'].includes(issue.state)&&(!Array.isArray(data.repositories)||data.repositories.some(repo=>repo.enabled&&repo.name===issue.repo))).length;
      if(queued){const link=document.createElement('a');link.href='/factory#columns';link.textContent=`${queued} queued`;link.title='Tracked issues waiting for Build, Review or Audit';link.setAttribute('aria-label',`${queued} queued ${queued===1?'issue':'issues'}`);fragment.append(link);}
      indicator.replaceChildren(fragment);indicator.hidden=!indicator.childElementCount;
    }catch(error){hide();}
    finally{clearTimeout(timer);timer=setTimeout(refresh,15000);}
  }
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)refresh();});
  refresh();
})();</script>"""

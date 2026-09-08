#!/usr/bin/env python3
"""forge-status — read-only, tailnet-only dashboard for the agent supervisor.

Security model: binds ONLY to the Tailscale IP (unreachable off-tailnet),
serves read-only data (tmux pane text, supervisor log tail, gh queue counts),
takes no write actions, has no dependencies beyond the stdlib.
"""
import json, re, subprocess, time, html, os, sqlite3
from pathlib import Path
from forge.config import load_repositories
from forge.activity import observe_tmux
from forge.observability import read_overview, read_repository
from forge.control import snapshot_path
from forge.scheduling import repo_state
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8787
HOME = str(Path.home())
WD = os.environ.get("FORGE_ROOT", f"{HOME}/ops/forge-watchdog")
SESS_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_cache = {}  # key -> (expires, value)


def sh(cmd, timeout=25):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    _cache[key] = (now + ttl, val)
    return val


def tmux_sessions():
    out = sh(["tmux", "ls", "-F", "#{session_name}"])
    present = set(out.split()) & allowed_sessions()
    observations = observe_tmux(present, state=Path(WD)/'state')
    return {session: {'busy': observation['state'] == 'busy',
                      'state': observation['state'], 'reason': observation['reason'],
                      'provider': observation.get('provider','unknown'),
                      'generation_id': observation.get('generation_id')}
            for session, observation in observations.items()
            if observation['state'] != 'exited'}


def repos():
    try:
        return [r.repo for r in load_repositories(Path(WD) / "repos.d") if r.enabled]
    except (ValueError, OSError):
        return []


def allowed_sessions():
    return {repo.split("/")[-1] + "-" + role for repo in repos() for role in ("build", "review", "audit")}


def slot_inventory():
    """Count configured survivors, including disabled repos, without capturing their panes."""
    from forge.capacity import read_sessions
    try:
        configured = {r.short + '-' + role for r in load_repositories(Path(WD)/'repos.d')
                      for role in ('build','review','audit')}
        names, _ = read_sessions()
        occupied = sorted(set(names) & configured)
        return {'status':'ok', 'observed_at':time.time(), 'occupied':len(occupied),
                'roles':{role:sum(s.endswith('-'+role) for s in occupied)
                         for role in ('build','review','audit')}}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'status':'unknown', 'observed_at':time.time()}


def gh_counts(repo):
    # Display the supervisor's interpretation, including unknown/stale state.
    unknown = {k: "?" for k in ("ready", "wip", "review", "audit", "sentback", "limbo", "held", "conflicts")}
    try:
        value = json.loads(snapshot_path(Path(WD) / "state", repo).read_text())
        if value["status"] != "ok" or time.time() - value["observed_at"] > 900:
            return unknown
        return value["counts"]
    except (OSError, ValueError, KeyError):
        return unknown


def notification_events():
    values = []
    for path in (Path(WD) / "state/notifications/outbox").glob("*.json"):
        try:
            event = json.loads(path.read_text())
            values.append(event)
        except (ValueError, OSError):
            continue
    return sorted(values, key=lambda e: e.get("created_at", 0))[-20:]




_cpu_previous = None


def host_health():
    """Current host telemetry, separate from historical admission decisions."""
    global _cpu_previous
    result = {'status': 'unknown', 'observed_at': time.time()}
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
                values[key] = int(value.strip().split()[0]) * 1024
        result.update(status='ok', memory_total_bytes=values['MemTotal'],
                      memory_available_bytes=values['MemAvailable'],
                      swap_used_bytes=values['SwapTotal'] - values['SwapFree'])
        cpu = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        current = (sum(cpu), cpu[3] + cpu[4])
        if _cpu_previous and current[0] > _cpu_previous[0]:
            result['cpu_busy_percent'] = round(100 * (1 - (current[1]-_cpu_previous[1])/(current[0]-_cpu_previous[0])), 1)
        _cpu_previous = current
        from forge.capacity import parse_pressure
        result['memory_pressure'] = parse_pressure(Path('/proc/pressure/memory').read_text())
    except (OSError, ValueError, KeyError, IndexError):
        result['status'] = 'unknown'
    raw = sh(['systemctl', '--user', 'show', 'forge-supervisor.service',
              'forge-activity.timer', 'forge-observability.timer', '--property=Id', '--property=ActiveState'])
    result['services'] = {}
    for block in raw.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if fields.get('Id'):
            result['services'][fields['Id']] = fields.get('ActiveState', 'unknown')
    return result

def current_work_context(repository_rows):
    """Join bounded queue references to local titles, never infer an assignment."""
    path = Path(WD) / 'state/observability.sqlite3'
    db = None
    try:
        if path.exists():
            db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
        for row in repository_rows:
            row['focus'] = {}
            for role in ('build', 'review', 'audit'):
                if not row[role]['up']:
                    continue
                if row.get('queue_status') != 'ok':
                    row['focus'][role] = {'basis': 'unknown', 'items': [], 'total': None}
                    continue
                work = row.get('work', {})
                claimed = row.get('schedule', {}).get('active', [])
                numbers = claimed if role == 'build' else work.get(role, [])
                basis = 'claimed' if role == 'build' and claimed else 'queued'
                if role == 'build' and not numbers:
                    numbers = work.get('sentback', []) + work.get('limbo', []) + row.get('schedule', {}).get('eligible', [])
                items = []
                for number in numbers[:3]:
                    item = {'number': number, 'title': None, 'kind': 'issue' if role == 'build' and number not in work.get('sentback', []) + work.get('limbo', []) else 'pr'}
                    if db:
                        stored = db.execute('SELECT data,observed_at FROM items WHERE repo=? AND number=?', (row['repo'], number)).fetchone()
                        if stored:
                            data = json.loads(stored[0])
                            item.update({key: data.get(key) for key in ('title', 'kind', 'updated_at')})
                            item['metadata_observed_at'] = stored[1]
                    observed_item = row.get('item_details', {}).get(str(number))
                    if isinstance(observed_item, dict):
                        item.update({key: observed_item[key] for key in ('title', 'kind', 'updated_at')
                                     if observed_item.get(key) is not None})
                        item['metadata_observed_at'] = row.get('observed_at')
                    items.append(item)
                row['focus'][role] = {'basis': basis, 'items': items, 'total': len(numbers)}
    except (sqlite3.Error, ValueError, OSError):
        # Missing enrichment cannot make live worker state or snapshot claims vanish.
        for row in repository_rows:
            row.setdefault('focus', {})
    finally:
        if db:
            db.close()

def recent_workflow_events(state, allowed, limit, before=None):
    """Newest enabled-repository events, with an exclusive backwards cursor."""
    path=Path(state)/'workflow.sqlite3'
    if not path.exists() or not allowed:
        return []
    conditions=['lower(repo) IN ('+','.join('?' for _ in allowed)+')']
    parameters=sorted(allowed)
    if before is not None:
        conditions.append('id<?');parameters.append(before)
    parameters.append(limit)
    from contextlib import closing
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT * FROM events WHERE '+' AND '.join(conditions)+' ORDER BY id DESC LIMIT ?',parameters)
        result=[]
        for row in rows:
            event=dict(row)
            for key in ('payload','result'):event[key]=json.loads(event[key])
            result.append(event)
        return result


def workflow_view(repo=None, after=0, limit=100, include_history=True, recent=False, before=None):
    """Read workflow authority without exposing disabled repositories or writing state."""
    allowed={value.lower() for value in repos()}
    if repo is not None and repo.lower() not in allowed:
        raise ValueError('Repository is not enabled')
    try:
        from forge.workflow import read_state, history
        value=read_state(Path(WD)/'state', repo=repo.lower() if repo else None)
        if value.get('status') not in {'ok','uninitialized'}:
            raise ValueError('Invalid workflow status')
        result={'status':value['status'],'read_at':time.time()}
        for key in ('issues','assignments','slots','repositories'):
            if not isinstance(value.get(key),list):
                raise ValueError('Invalid workflow collection')
            result[key]=[row for row in value[key] if isinstance(row,dict) and str(row.get('repo','')).lower() in allowed]
        if include_history:
            rows=(recent_workflow_events(Path(WD)/'state', {repo.lower()} if repo else allowed, limit+1, before)
                  if recent else history(Path(WD)/'state',repo=repo.lower() if repo else None,after=after,limit=limit+1))
            page=rows[:limit]
            result.update(events=[row for row in page if str(row.get('repo','')).lower() in allowed],
                          next_after=page[-1]['id'] if page else after,has_more=len(rows)>limit)
            if recent:result['next_before']=page[-1]['id'] if page else before
        return result
    except (ImportError,OSError,ValueError,TypeError,KeyError,RuntimeError,sqlite3.Error):
        return {'status':'unavailable','reason':'Workflow state could not be read','issues':[],
                'assignments':[],'slots':[],'repositories':[],'events':[],'next_after':after,'has_more':False}


def dashboard_workflow(value, repositories, flow, *, now=None):
    """Compatibility projection: queued inventory is not ready pipeline demand.

    Keep the complete durable inventory on /api/workflow. Filtering /api/status
    also fixes clients still running an older cached dashboard script.
    """
    now=time.time() if now is None else now
    ready=set()
    fresh={r['repo'].lower():r for r in repositories if r.get('queue_status')=='ok'
           and type(r.get('observed_at')) in (int,float) and 0<=now-r['observed_at']<=300}
    for repo,row in fresh.items():
        ready.update((repo,n) for n in row.get('work',{}).get('ready',[]) if type(n) is int)
        ready.update((repo,n) for n in row.get('schedule',{}).get('eligible',[]) if type(n) is int)
    ready.update((i['repo'].lower(),i['number']) for i in flow.get('issues',[])
                 if i.get('state')=='ready' and i['repo'].lower() in fresh)
    slots={s['assignment_id']:s for s in value.get('slots',[]) if s.get('assignment_id') and s.get('state')!='free'}
    assigned=set()
    for a in value.get('assignments',[]):
        slot=slots.get(a['id'])
        if (a.get('role')=='build' and a.get('state')!='retired' and slot
                and slot.get('session')==a.get('session') and slot.get('repo')==a.get('repo')):
            assigned.update((a['repo'].lower(),n) for n in a.get('issue_numbers',[]))
    issues=[]
    for item in value.get('issues',[]):
        key=(item['repo'].lower(),item['number']);stage=item.get('stage')
        if stage in {'build','review','audit'}:issues.append(item)
        elif stage in {'queued','backlog'} and key in assigned:issues.append({**item,'stage':'build'})
        elif stage in {'queued','backlog'} and key in ready:issues.append(item)
    return {**value,'issues':issues,'projection':'pipeline','inventory_issue_count':len(value.get('issues',[]))}


def worker_fault_context(state, blockers, flow, activity):
    """Related historical queue context, never inferred worker assignment."""
    path=Path(state)/'activity.sqlite3'
    if not path.exists():
        return
    db=None
    try:
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        for session,blocker in blockers.items():
            if blocker.get('kind')!='cleanup_retained' or blocker.get('context'):
                continue
            worker=activity.get('sessions',{}).get(session,{})
            identity=blocker.get('evidence',{}).get('generation_id')
            if not identity or identity!=worker.get('generation_id') or not worker.get('session_started_at'):
                continue
            created=db.execute("SELECT id FROM events WHERE session=? AND event='session_created' ORDER BY id DESC LIMIT 1",(session,)).fetchone()
            if not created:
                continue
            latest=None
            role=session.rsplit('-',1)[-1]
            field='claimed' if role=='build' else 'queued'
            for repo,at,raw in db.execute("SELECT repo,at,metadata FROM events WHERE session=? AND event='pipeline_observed' AND id>? AND at>=? ORDER BY id DESC LIMIT 100",(session,created[0],worker['session_started_at'])):
                numbers=json.loads(raw).get(field,[])
                if numbers:
                    latest=(repo,at,numbers)
                    break
            if latest is None:
                continue
            repo,at,numbers=latest
            if not isinstance(repo,str) or not isinstance(numbers,list) or len(numbers)!=1 or type(numbers[0]) is not int or numbers[0]<=0:
                continue
            candidates=[issue for issue in flow.get('issues',[]) if isinstance(issue.get('repo'),str) and issue['repo'].lower()==repo.lower()
                        and (issue.get('number')==numbers[0] if role=='build' else numbers[0] in issue.get('pr_numbers',[]))]
            context={'repo':repo,'basis':'Previously claimed' if role=='build' else 'Previously queued','observed_at':at}
            if role!='build':
                context['pr']=numbers[0]
            if len(candidates)==1 and type(candidates[0].get('number')) is int and candidates[0]['number']>0:
                context['issue']=candidates[0]['number']
            if context.get('pr') or context.get('issue'):
                blocker['context']=context
    except (OSError,ValueError,TypeError,KeyError,AttributeError,sqlite3.Error):
        return
    finally:
        if db:
            db.close()


def status():
    sess = tmux_sessions()
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "repos": [], "mode": "legacy-contained"}
    for repo in repos():
        short = repo.split("/")[-1]
        r = {"repo": repo, "short": short, "counts": gh_counts(repo)}
        try:
            snap = json.loads(snapshot_path(Path(WD) / "state", repo).read_text())
            r["schedule"] = snap.get("schedule", {})
            r["observed_at"] = snap.get("observed_at")
            r["queue_status"] = snap.get("status", "unknown")
            if r["queue_status"] == "ok" and time.time() - (r["observed_at"] or 0) > 900:
                r["queue_status"] = "stale"
            r["queue_reason"] = snap.get("reason", "")
            r["work"] = snap.get("work", {})
            r["item_details"] = snap.get("item_details", {})
        except (OSError, ValueError):
            r["schedule"] = {}
            r["queue_status"] = "unknown"
        try:
            r["triage"] = json.loads(repo_state(Path(WD) / "state", repo).read_text()).get("last_result", {})
        except (OSError, ValueError):
            r["triage"] = {}
        for role in ("build", "review", "audit"):
            s = sess.get(f"{short}-{role}")
            r[role] = {"up": s is not None, "busy": bool(s and s["busy"]),
                       "state": s.get("state", "unknown") if s else "exited",
                       "provider": s.get("provider", "unknown") if s else None,
                       "generation_id": s.get("generation_id") if s else None}
        out["repos"].append(r)
    current_work_context(out["repos"])
    out["log"] = sh(["tail", "-n", "40", f"{WD}/logs/supervisor.log"]).splitlines()
    events = notification_events()
    out["notification_events"] = events
    out["notifications"] = [e["message"] + " [" + ", ".join(c + ":" + r["status"] for c, r in e["channels"].items()) + "]" for e in events]
    if not events:
        out["notifications"] = sh(["tail", "-n", "20", f"{WD}/logs/notifications.log"]).splitlines()
    try:
        out["health"] = json.loads((Path(WD) / "state/legacy-health/guard.json").read_text())
        if not isinstance(out["health"], dict):
            raise ValueError("Invalid provider health")
        out["health"]["status"] = "ok"
    except (OSError, ValueError):
        out["health"] = {"status": "unknown", "sessions": {}, "account_until": 0}
    try:
        out["capacity"] = json.loads((Path(WD) / "state/capacity.json").read_text())
        if not isinstance(out["capacity"], dict):
            raise ValueError("invalid capacity decision")
    except (OSError, ValueError):
        out["capacity"] = {"status": "unknown", "reason": "No admission decision recorded yet"}
    out['host'] = host_health()
    try:
        from forge.activity import read_status
        out['activity'] = read_status(Path(WD) / 'state', allowed_sessions=allowed_sessions())
    except (ImportError, OSError, ValueError, sqlite3.Error):
        out['activity'] = {'status': 'unavailable', 'sessions': {}, 'events': []}
    try:
        from forge.issue_flow import read_flow
        out['issue_flow'] = read_flow(Path(WD) / 'state', out['repos'], health=out['health'])
    except (ImportError, OSError, ValueError, sqlite3.Error):
        out['issue_flow'] = {'status': 'unavailable', 'issues': [], 'counts': {}}
    try:
        from forge.blockers import read_blockers
        out['session_blockers'] = read_blockers(Path(WD) / 'state', out['repos'], out['health'], out['activity'])
    except (ImportError, OSError, ValueError, sqlite3.Error):
        out['session_blockers'] = {}
    worker_fault_context(Path(WD)/'state',out['session_blockers'],out['issue_flow'],out['activity'])
    out['workflow'] = dashboard_workflow(workflow_view(include_history=False),out['repos'],out['issue_flow'])
    out['slots'] = slot_inventory()
    try:
        from forge.routing import read_routing
        routing = read_routing(Path(WD)/'state')
        out['routing'] = {'status':'ok', 'last_provider':routing.get('last_provider'),
                          'next_provider':routing.get('next_provider'),
                          'providers':{provider:{key:value for key,value in details.items()
                                      if key in {'reason','last_failure','blocked_until','probe_until'}}
                                       for provider,details in routing.get('providers',{}).items()
                                       if provider in {'claude','codex'} and isinstance(details,dict)}}
    except (ImportError, OSError, ValueError, RuntimeError):
        out['routing'] = {'status':'unknown'}
    return out


def repository_detail(repo):
    result = read_repository(Path(WD) / "state", repo)
    # Raw provider envelopes remain in SQLite. Avoid duplicating them in every
    # dashboard response while retaining all displayed content and source links.
    for item in result.get("items", []):
        for key in ("raw", "raw_pr", "raw_pr_summary"):
            item.pop(key, None)
        for evidence in item.get("evidence", []):
            raw = evidence.pop("raw", {})
            evidence["context"] = {key: raw[key] for key in
                                   ("path", "line", "original_line", "diff_hunk", "commit_id") if key in raw}
    return result


PAGE = Path(__file__).with_name("dashboard.html").read_text()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body, ctype="application/json"):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            self._send(json.dumps(cached("status", 10, status)))
        elif u.path == "/api/workflow":
            query=parse_qs(u.query)
            repo=query.get('repo',[None])[0]
            after=query.get('after',['0'])[0]
            limit=query.get('limit',['100'])[0]
            recent=query.get('recent',['0'])[0];before=query.get('before',[None])[0]
            if (set(query)-{'repo','after','limit','recent','before'} or any(len(values)!=1 for values in query.values()) or
                    repo is not None and repo.lower() not in {r.lower() for r in repos()} or
                    recent not in {'0','1'} or (before is not None and (recent!='1' or not re.fullmatch(r'[0-9]{1,18}',before))) or
                    (recent=='1' and 'after' in query) or not re.fullmatch(r'[0-9]{1,18}',after) or not re.fullmatch(r'[0-9]{1,3}',limit) or not 1<=int(limit)<=100):
                self.send_error(400)
                return
            self._send(json.dumps(workflow_view(repo,int(after),int(limit),recent=True,before=int(before) if before else None)
                                  if recent=='1' else workflow_view(repo,int(after),int(limit))))
        elif u.path == "/api/history":
            raw_days = parse_qs(u.query).get("days", ["30"])[0]
            if raw_days not in {"7", "30", "90"}:
                self.send_error(400)
                return
            self._send(json.dumps(read_overview(Path(WD) / "state", days=int(raw_days), repos=repos())))
        elif u.path == "/api/repository":
            repo = parse_qs(u.query).get("repo", [""])[0]
            if repo not in repos():
                self.send_error(400)
                return
            self._send(json.dumps(repository_detail(repo)))
        elif u.path == "/api/pane":
            s = parse_qs(u.query).get("s", [""])[0]
            if not SESS_RE.fullmatch(s) or s not in allowed_sessions():
                self.send_error(400)
                return
            self._send(sh(["tmux", "capture-pane", "-p", "-t", f"={s}:1.1", "-S", "-120"]), "text/plain; charset=utf-8")
        else:
            self.send_error(404)


if __name__ == "__main__":
    ip = sh(["tailscale", "ip", "-4"]).strip().split("\n")[0]
    if not ip:
        raise SystemExit("no tailscale ip — refusing to bind")
    print(f"forge-status on http://{ip}:{PORT}")
    ThreadingHTTPServer((ip, PORT), H).serve_forever()

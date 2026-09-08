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
HOME = "/home/luc"
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
                      'provider': observation.get('provider','unknown')}
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
                       "provider": s.get("provider", "unknown") if s else None}
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

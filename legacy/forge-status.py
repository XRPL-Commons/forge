#!/usr/bin/env python3
"""Loopback JSON API behind the operator UI (ui/), for the durable factory.

Only the UI may call it (UPSTREAM_SECRET). Writes also require a same-origin
JSON request and a per-process capability token. The server queues intent;
only the locked supervisor acts on it.
"""
import json, re, subprocess, os, secrets, hmac, threading, ipaddress
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8787
HOME = str(Path.home())
WD = os.environ.get("FORGE_ROOT", f"{HOME}/ops/forge-watchdog")
CONTROLS_TOKEN = secrets.token_urlsafe(32)
# Proof that a request came through the operator UI, not from another local
# process: a 0600 file only this account (and the UI container, read-only) can
# read. Loaded by __main__; None only when imported by tests.
UPSTREAM_SECRET = None


def upstream_secret(path):
    """Read the UI↔API secret, creating it (0600) on first start."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    info = path.lstat()
    if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit(f'{path} must be a private regular file owned by this account')
    value = path.read_text().strip()
    if len(value) < 32:
        raise SystemExit(f'{path} does not hold a usable secret')
    return value




def settings_service():
    from forge.settings import Settings
    from forge.onboarding import Onboarding
    return Settings(WD) if (Path(WD)/'state/factory-v2.enabled').exists() else Onboarding(WD).settings()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body, ctype="application/json", headers=None):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(data)

    def _browser(self):
        from http.cookies import SimpleCookie, CookieError
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            value=cookie['forge_setup'].value if 'forge_setup' in cookie else ''
            return value if re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value) else ''
        except CookieError:
            return ''

    def _redirect(self, target):
        self.send_response(303)
        self.send_header('Location',target)
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Length','0')
        self.end_headers()

    def _upstream(self):
        # Every request must come through the operator UI (see UPSTREAM_SECRET).
        return UPSTREAM_SECRET is None or hmac.compare_digest(self.headers.get('X-Forge-Upstream',''),UPSTREAM_SECRET)

    def _public_host(self):
        # Behind the operator UI, Host is the loopback upstream; the UI forwards
        # the name the browser used after validating it against its own binds.
        # Only the UI can send it: requests without the upstream secret never get here.
        forwarded=self.headers.get('X-Forwarded-Host','')
        if re.fullmatch(r'[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::[0-9]{1,5})?',forwarded):
            return forwarded
        return self.headers.get('Host','')

    def _trusted_host(self):
        # Reject DNS rebinding even when Origin and Host agree with each other.
        # Only the address actually bound by this tailnet server can read its
        # capability token or submit an operator action.
        address,port=self.server.server_address[:2]
        expected=f'[{address}]:{port}' if ':' in address else f'{address}:{port}'
        return self.headers.get('Host','')==expected

    def do_POST(self):
        if not self._trusted_host() or not self._upstream():
            self.send_error(403);return
        if self.path not in {'/api/settings', '/api/settings/sync', '/api/factory/action', '/api/runtime', '/api/onboarding', '/api/repositories/check', '/api/repositories/enable', '/api/repositories/deployment', '/api/issues/action', '/api/issues/refresh', '/api/issues/discussion'}:
            self.send_error(404);return
        # An attacker may cause a browser to send a form, but cannot read the
        # dashboard token or set this header cross-origin without CORS approval.
        token=self.headers.get('X-Forge-Action','')
        origin=urlparse(self.headers.get('Origin',''))
        host=self.headers.get('Host','')
        if (not hmac.compare_digest(token,CONTROLS_TOKEN) or origin.scheme!='http'
                or origin.netloc!=host or origin.path or origin.query or origin.fragment
                or self.headers.get('Sec-Fetch-Site','same-origin')!='same-origin'):
            self.send_error(403);return
        if (self.headers.get('Content-Type','').split(';')[0].strip()!='application/json'
                or self.headers.get('Transfer-Encoding')):
            self.send_error(415);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            maximum = 262144 if self.path in ('/api/settings', '/api/runtime') else 8192
            if not 0<length<=maximum:raise ValueError('invalid_request_size')
            value=json.loads(self.rfile.read(length))
            if self.path == '/api/onboarding':
                from forge.onboarding import Onboarding
                setup=Onboarding(WD)
                if not isinstance(value,dict):raise ValueError('Expected a setup action')
                action=value.pop('action',None)
                fields={'github':{'role','owner','account_type'},'refresh':set(),'repository':{'repo_id'},
                        'check':{'revision'},'start':{'revision'},'reset':{'generation'}}
                if action not in fields or set(value)!=fields[action]:raise ValueError('Invalid setup action')
                result={}
                if action=='github':result=setup.begin(**value,origin='http://'+self._public_host(),browser=self._browser())
                elif action=='refresh':result=setup.refresh()
                elif action=='repository':setup.select(value['repo_id'])
                else:result={'request':setup.queue(action,**value)}
                self._send(json.dumps({'ok':True,**result}));return
            if (Path(WD)/'state/factory-paused.json').exists():
                raise ValueError('Factory reset is in progress')
            setup_state=Path(WD)/'state/onboarding/setup.json'
            if self.path in ('/api/settings','/api/runtime') and setup_state.is_file() and json.loads(setup_state.read_text()).get('phase')!='active':
                raise ValueError('Use guided setup until the factory has started')
            if self.path == '/api/issues/discussion':
                from forge.factory.discussions import Discussions
                service = Discussions(WD)
                if not isinstance(value, dict): raise ValueError('Expected a conversation action')
                action = value.get('action')
                fields = {'send': {'key', 'action', 'message', 'request_id'}, 'cancel': {'key', 'action'}, 'apply': {'key', 'action', 'message_id'}}
                if action not in fields or set(value) != fields[action]: raise ValueError('Invalid conversation action')
                result = None
                if action == 'send': result = service.send(value['key'], value['message'], value['request_id'])
                elif action == 'cancel': service.cancel(value['key'])
                else: result = service.apply(value['key'], value['message_id'])
                self._send(json.dumps({'ok': True, 'request': result}));return
            if self.path == '/api/issues/action':
                from forge.factory.issue_actions import request
                if not isinstance(value,dict) or set(value)!={'key','action','revision','reason'}:
                    raise ValueError('Expected issue, action, current revision, and decision')
                self._send(json.dumps({'ok':True,'request':request(WD,**value)}));return
            if self.path == '/api/issues/refresh':
                from forge.factory.issues import IssueInventory
                if value!={}:raise ValueError('Expected an empty refresh request')
                IssueInventory(WD).start_refresh(force=True)
                self._send(json.dumps({'ok':True}));return
            if self.path == '/api/factory/action':
                from forge.factory.control import queue
                if not isinstance(value, dict) or set(value) != {'action', 'target', 'reason'}:
                    raise ValueError('Expected action, target, and reason')
                identifier=queue(WD, **value)
                self._send(json.dumps({'ok':True,'request':identifier}));return
            if self.path == '/api/runtime':
                from forge.factory.control import save_runtime
                if not isinstance(value, dict) or set(value) != {'config', 'revision'}:
                    raise ValueError('Expected config and revision')
                result=save_runtime(WD, **value)
                self._send(json.dumps({**result,'ok':True,'controls_token':CONTROLS_TOKEN}));return
            if self.path == '/api/repositories/check':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value,dict) or set(value)!={'repo_id','revision'} or (value['repo_id'] is not None and type(value['repo_id']) is not int):
                    raise ValueError('Expected repository identity and current revision')
                RepositorySetup(settings_service()).start_check(value['repo_id'],value['revision'])
                self._send(json.dumps({'ok':True}));return
            if self.path == '/api/repositories/enable':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value,dict) or set(value)!={'repo_id','revision'} or type(value['repo_id']) is not int:
                    raise ValueError('Expected repository identity and current revision')
                result=RepositorySetup(settings_service()).enable(value['repo_id'],value['revision'])
                self._send(json.dumps({**result,'ok':True,'controls_token':CONTROLS_TOKEN}));return
            if self.path == '/api/repositories/deployment':
                from forge.repository_setup import RepositorySetup
                fields={'confirm':{'domain'},'reject':{'reason'},'reread':set()}
                action=value.get('action') if isinstance(value,dict) else None
                if (action not in fields or set(value)!={'repo_id','revision','action'}|fields[action]
                        or type(value['repo_id']) is not int):
                    raise ValueError('Expected repository identity, current revision, and a deployment decision')
                result=RepositorySetup(settings_service()).deployment(**value)
                self._send(json.dumps({**result,'ok':True,'controls_token':CONTROLS_TOKEN}));return
            if self.path == '/api/settings/sync':
                if value != {}:raise ValueError('Expected an empty sync request')
                settings_service().start_refresh()
                self._send(json.dumps({'ok':True,'message':'Access refresh requested'}));return
            if self.path == '/api/settings':
                if not isinstance(value,dict) or set(value) != {'config','revision'}:
                    raise ValueError('Expected config and revision')
                result=settings_service().save(value['config'],value['revision'])
                result.update(ok=True,controls_token=CONTROLS_TOKEN)
                self._send(json.dumps(result));return
            raise ValueError('invalid_action_request')
        except (ValueError,TypeError) as exc:
            self._send(json.dumps({'ok':False,'reason':str(exc)}))
        except (OSError,ImportError,subprocess.SubprocessError):
            self._send(json.dumps({'ok':False,'reason':'Operation unavailable; check server configuration'}))

    def do_GET(self):
        if not self._trusted_host() or not self._upstream():
            self.send_error(403);return
        u = urlparse(self.path)
        if u.path.startswith('/api/') and u.path!='/api/onboarding' and (Path(WD)/'state/factory-paused.json').exists():
            self._send(json.dumps({'ok':False,'reason':'Factory reset is in progress'}));return
        if u.path == '/onboarding/github/callback':
            from forge.onboarding import Onboarding
            try:
                query=parse_qs(u.query)
                if set(query)!={'state','code'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Invalid GitHub callback')
                target=Onboarding(WD).callback(query['state'][0],query['code'][0],self._browser())
                self._redirect(target)
            except (ValueError,OSError,RuntimeError):
                self._redirect('/onboarding?connection_error=1')
            return
        if u.path == '/api/onboarding':
            from forge.onboarding import Onboarding
            try:
                self._send(json.dumps({'ok':True,**Onboarding(WD).view(),'controls_token':CONTROLS_TOKEN}))
            except (ValueError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Setup state is unavailable; inspect the supervisor service'}))
        elif u.path == '/api/issues/discussion':
            from forge.factory.discussions import Discussions
            try:
                query = parse_qs(u.query)
                if set(query) - {'issue', 'before'} or 'issue' not in query or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid conversation view')
                result = Discussions(WD).view(query['issue'][0], int(query['before'][0]) if 'before' in query else None)
                self._send(json.dumps({'ok': True, **result, 'controls_token': CONTROLS_TOKEN}))
            except (ValueError, KeyError, OSError, RuntimeError):
                self._send(json.dumps({'ok': False, 'reason': 'Conversation unavailable. Refresh the issue and try again.'}))
        elif u.path == '/api/issues':
            from forge.factory.issues import IssueInventory
            try:
                query=parse_qs(u.query)
                if set(query)-{'repo_id','state','q','page','issue'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Invalid issue view')
                service=IssueInventory(WD)
                service.start_refresh()
                result=service.view(repo_id=int(query['repo_id'][0]) if 'repo_id' in query else None,
                                    state=query.get('state',['open'])[0],query=query.get('q',[''])[0],
                                    page=int(query.get('page',['1'])[0]),key=query.get('issue',[None])[0])
                self._send(json.dumps({**result,'ok':True,'controls_token':CONTROLS_TOKEN}))
            except (OSError,ValueError,KeyError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Issue inventory is unavailable. Refresh to try again.'}))
        elif u.path == '/api/factory':
            try:
                if (Path(WD)/'state/factory-paused.json').exists():raise ValueError('Resetting')
                from forge.factory.control import view
                self._send(json.dumps({**view(WD),'ok':True,'controls_token':CONTROLS_TOKEN}))
            except (ValueError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Factory state is unavailable; reconcile the durable ledger.'}))
        elif u.path == '/api/factory/output':
            try:
                if (Path(WD)/'state/factory-paused.json').exists():raise ValueError('Resetting')
                from forge.factory.ledger import Ledger
                from forge.factory.contracts import identifier
                query=parse_qs(u.query)
                if set(query) != {'id'} or len(query['id']) != 1:raise ValueError('Invalid execution')
                job=Ledger(Path(WD)/'state'/'factory').read()['jobs'][identifier(query['id'][0])]
                self._send(json.dumps({'ok':True,'logs':(job.get('capture') or job.get('observation') or {}).get('logs','')}))
            except (ValueError,KeyError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Execution output unavailable'}))
        elif u.path in ('/api/repositories','/api/repositories/ruleset'):
            from forge.repository_setup import RepositorySetup
            try:
                query=parse_qs(u.query)
                allowed={'repo_id','revision'} if u.path.endswith('/ruleset') else {'repo_id'}
                if set(query)-allowed or any(len(v)!=1 for v in query.values()):raise ValueError('Invalid repository request')
                repo_id=int(query['repo_id'][0]) if 'repo_id' in query else None
                service=RepositorySetup(settings_service())
                if u.path.endswith('/ruleset'):
                    if repo_id is None or 'revision' not in query:raise ValueError('Select and check a repository first')
                    rules,filename=service.download(repo_id,query['revision'][0])
                    self._send(json.dumps(rules,indent=2)+'\n','application/json',{'Content-Disposition':'attachment; filename="'+filename+'"'})
                else:self._send(json.dumps({'ok':True,**service.view(repo_id),'controls_token':CONTROLS_TOKEN}))
            except (OSError,ValueError,TypeError,KeyError):
                self._send(json.dumps({'ok':False,'reason':'Repository setup changed or is unavailable. Check again.'}))
        elif u.path == '/api/settings':
            try:
                if (Path(WD)/'state/factory-paused.json').exists():raise ValueError('Resetting')
                result=settings_service().view()
                result.update(ok=True,controls_token=CONTROLS_TOKEN)
                self._send(json.dumps(result))
            except (OSError,ValueError,TypeError,KeyError):
                self._send(json.dumps({'ok':False,'reason':'Settings unavailable. Validate factory.json on the server.'}))
        else:
            self.send_error(404)


if __name__ == "__main__":
    # The operator UI is the network front door; this API listens on loopback.
    ip = os.environ.get('FORGE_BIND_ADDRESS', '')
    try:
        address=ipaddress.ip_address(ip)
        port=int(os.environ.get('FORGE_PORT',str(PORT)))
        if address.version != 4 or address.is_unspecified or not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise SystemExit('Set FORGE_BIND_ADDRESS=127.0.0.1 and a valid FORGE_PORT; bin/forge setup writes both')
    if not address.is_loopback and os.environ.get('FORGE_ALLOW_NETWORK_BIND') != '1':
        raise SystemExit('The status API binds loopback only; the operator UI serves the network. '
                         'Set FORGE_ALLOW_NETWORK_BIND=1 only for a legacy installation without the UI.')
    UPSTREAM_SECRET = upstream_secret(os.environ.get('FORGE_UPSTREAM_SECRET_FILE') or Path(WD) / 'state/ui-upstream.secret')
    print(f"forge-status on http://{ip}:{port}")
    threading.Thread(target=settings_service().watch,daemon=True).start()
    ThreadingHTTPServer((ip, port), H).serve_forever()

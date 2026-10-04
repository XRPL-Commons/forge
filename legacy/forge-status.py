#!/usr/bin/env python3
"""Loopback JSON API behind the operator UI (ui/), for the durable factory.

Only the UI may call it (UPSTREAM_SECRET). Writes also require a same-origin
JSON request and a per-process capability token. The server queues intent;
only the locked supervisor acts on it.
"""
import hashlib
import json, re, subprocess, os, secrets, hmac, threading, ipaddress
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8787
HOME = str(Path.home())
WD = os.environ.get("FORGE_ROOT", f"{HOME}/ops/forge-watchdog")
from forge.view_cache import ViewCache
VIEWS = ViewCache()
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

    def _send(self, body, ctype="application/json", headers=None, status=200, etag=None):
        data = body if isinstance(body, bytes) else body.encode()
        # Validators are private to each client; no response is stored by a
        # browser/proxy cache. Include the action token in the digest so a
        # service restart always sends the new capability with a fresh body.
        etag = etag or ('W/"' + hashlib.sha256(data).hexdigest() + '"' if status == 200 and self.command == 'GET' and ctype == 'application/json' else None)
        if etag and self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'private, no-store')
            self.end_headers()
            return
        self.send_response(status)
        if etag: self.send_header('ETag', etag)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(data)

    def _cached(self, key, build):
        etag, body = VIEWS.response(WD, key, build, self._action_token, self.headers.get('If-None-Match'))
        self._send(body or b'', etag=etag)

    def _browser(self):
        from http.cookies import SimpleCookie, CookieError
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            value=cookie['forge_setup'].value if 'forge_setup' in cookie else ''
            return value if re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value) else ''
        except CookieError:
            return ''

    def _redirect(self, target, headers=()):
        self.send_response(303)
        self.send_header('Location',target)
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Length','0')
        for key, value in headers:
            self.send_header(key, value)
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

    def _operator(self):
        from forge.operator_auth import OperatorAuth, read_cookie, SESSION_COOKIE
        self._auth = OperatorAuth(WD)
        self._auth_config = self._auth.config()
        self._session_token = read_cookie(self.headers.get('Cookie', ''), SESSION_COOKIE)
        self._session = self._auth.session(self._session_token) if self._auth_config else None
        self._action_token = self._session['csrf'] if self._session else CONTROLS_TOKEN
        if self._auth_config and self._public_host() != urlparse(self._auth_config['origin']).netloc:
            self.send_error(403)
            return False
        return True

    def _operator_guard(self):
        try:
            return self._operator()
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            self.send_error(503)
            return False

    def _signed_in(self):
        if self._auth_config and not self._session:
            self.send_error(401)
            return False
        return True

    def _auth_get(self, url):
        from forge.operator_auth import read_cookie, FLOW_COOKIE, SESSION_COOKIE, cookie, failure_reason
        if url.path == '/api/auth/session':
            self._send(json.dumps({'ok': True, 'enabled': bool(self._auth_config),
                                  'operator': self._session['operator'] if self._session else None,
                                  **({'controls_token': self._action_token} if self._session else {})}))
            return True
        if url.path not in ('/auth/github/start', '/auth/github/callback'):
            return False
        try:
            if not self._auth_config:
                self.send_error(404)
                return True
            if url.path == '/auth/github/start':
                query = parse_qs(url.query)
                target, value = self._auth.begin(query.get('return_to', ['/'])[0])
                self._redirect(target, [('Set-Cookie', value)])
            else:
                query = parse_qs(url.query, keep_blank_values=True)
                if set(query) not in ({'state', 'code'}, {'state', 'code', 'iss'}) or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid callback')
                if 'iss' in query and query['iss'] != ['https://github.com/login/oauth']:
                    raise ValueError('Invalid callback issuer')
                value, target = self._auth.callback(query['state'][0], query['code'][0],
                                            read_cookie(self.headers.get('Cookie', ''), FLOW_COOKIE), self._session_token, with_target=True)
                self._redirect(target, [('Set-Cookie', value), ('Set-Cookie', cookie(FLOW_COOKIE))])
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            print(json.dumps({'event': 'operator_sign_in_failed', 'reason': failure_reason(error)}), flush=True)
            self._redirect('/login?error=sign_in', [('Set-Cookie', cookie(FLOW_COOKIE))])
        return True

    def _client_post(self):
        from forge.client_connections import ClientConnections, ConnectionError, fields, need
        from forge.operator_auth import OperatorAuth
        paths = {'/api/client/device', '/api/client/token', '/api/client/refresh',
                 '/api/client/status', '/api/client/disconnect', '/api/client/requirements',
                 '/api/client/authorize', '/api/client/read', '/api/client/operation'}
        if self.path not in paths:
            return False
        try:
            # These routes use client credentials only. Browser cookies, UI action
            # tokens and cross-origin fetches never authorize a client request.
            need(not self.headers.get('Origin') and not self.headers.get('Cookie'), status=403)
            need(self.headers.get('Content-Type', '').split(';')[0].strip() == 'application/json'
                 and not self.headers.get('Transfer-Encoding'), status=415)
            length = int(self.headers.get('Content-Length', '0'))
            need(0 < length <= 16384, status=413)
            value = json.loads(self.rfile.read(length))
            auth = OperatorAuth(WD)
            config = auth.config()
            need(config and self._public_host() == urlparse(config['origin']).netloc,
                 'sign_in_required', 'Configure HTTPS operator sign-in before connecting an agent.', 403)
            clients = ClientConnections(WD, auth=auth)
            authorization = self.headers.get('Authorization', '')
            bearer = authorization[7:] if authorization.startswith('Bearer ') else ''
            if self.path == '/api/client/device':
                result = clients.begin(value)
            elif self.path == '/api/client/token':
                result = clients.poll(value)
            elif self.path == '/api/client/refresh':
                fields(value, ('refresh_token',))
                result = clients.refresh(value['refresh_token'])
            elif self.path == '/api/client/status':
                fields(value, ())
                result = clients.status(bearer)
            elif self.path == '/api/client/disconnect':
                fields(value, ())
                clients.disconnect(bearer)
                result = {'disconnected': True}
            elif self.path == '/api/client/authorize':
                fields(value, ('repository', 'instance', 'generation', 'capability'))
                clients.authorize(bearer, value['repository'], value['capability'], value['instance'], value['generation'])
                result = {'authorized': True}
            elif self.path in ('/api/client/read', '/api/client/operation'):
                from forge.client_repositories import ClientRepositories, OPERATIONS
                # Fence before opening repository state, including malformed or
                # out-of-scope requests. Service methods repeat the check.
                capability = OPERATIONS.get(value.get('action'), 'repositories:read') if isinstance(value, dict) else 'repositories:read'
                clients.authorize(bearer, value['repository'], capability, value['instance'], value['generation'])
                service = ClientRepositories(settings_service(), clients)
                result = service.read(bearer, value) if self.path.endswith('/read') else service.operation(bearer, value)
            else:
                fields(value, ('repository', 'instance', 'generation'))
                clients.authorize(bearer, value['repository'], 'repositories:read', value['instance'], value['generation'])
                from forge.repository_requirements import describe
                result = describe(settings_service(), value['repository'])
                # Recheck after collecting data, including any factory reset.
                clients.authorize(bearer, value['repository'], 'repositories:read', value['instance'], value['generation'])
            self._send(json.dumps({'ok': True, **result}))
        except ConnectionError as exc:
            self._send(json.dumps({'ok': False, 'error': exc.code, 'reason': str(exc)}), status=exc.status)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            self._send(json.dumps({'ok': False, 'error': 'temporarily_unavailable',
                                   'reason': 'Client connection could not be verified.'}), status=503)
        return True

    def do_POST(self):
        if not self._trusted_host() or not self._upstream():
            self.send_error(403);return
        if self._client_post():
            return
        if not self._operator_guard() or not self._signed_in():
            return
        if self.path not in {'/api/connections/preview', '/api/connections/decide', '/api/connections/revoke', '/api/auth/logout', '/api/settings/sync', '/api/factory/action', '/api/runtime', '/api/onboarding', '/api/repositories/check', '/api/repositories/enable', '/api/repositories/disable', '/api/repositories/deployment', '/api/repositories/environment', '/api/issues/action', '/api/issues/environment', '/api/issues/refresh', '/api/issues/discussion'}:
            self.send_error(404);return
        # An attacker may cause a browser to send a form, but cannot read the
        # dashboard token or set this header cross-origin without CORS approval.
        token=self.headers.get('X-Forge-Action','')
        origin=urlparse(self.headers.get('Origin',''))
        host=self.headers.get('Host','')
        if (not hmac.compare_digest(token,self._action_token) or origin.scheme!='http'
                or origin.netloc!=host or origin.path or origin.query or origin.fragment
                or self.headers.get('Sec-Fetch-Site','same-origin')!='same-origin'):
            self.send_error(403);return
        if (self.headers.get('Content-Type','').split(';')[0].strip()!='application/json'
                or self.headers.get('Transfer-Encoding')):
            self.send_error(415);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            maximum = 262144 if self.path == '/api/runtime' else 8192
            if not 0<length<=maximum:raise ValueError('invalid_request_size')
            value=json.loads(self.rfile.read(length))
            if self._session:
                self._auth.audit(self._session, self.path)
            if self.path.startswith('/api/connections/'):
                from forge.client_connections import ClientConnections, fields
                clients = ClientConnections(WD, auth=self._auth)
                if self.path == '/api/connections/preview':
                    fields(value, ('code',))
                    result = clients.preview(self._session_token, value['code'])
                elif self.path == '/api/connections/decide':
                    result = clients.decide(self._session_token, value)
                else:
                    fields(value, ('id',))
                    clients.revoke(self._session_token, value['id'])
                    result = {'revoked': True}
                self._send(json.dumps({'ok': True, **result}))
                return
            if self.path == '/api/auth/logout':
                from forge.operator_auth import cookie, SESSION_COOKIE
                if value != {} or not self._session:
                    raise ValueError('Expected an authenticated logout request')
                self._auth.logout(self._session_token)
                self._send(json.dumps({'ok': True}), headers={'Set-Cookie': cookie(SESSION_COOKIE)})
                return
            if self.path == '/api/onboarding':
                from forge.onboarding import Onboarding
                setup=Onboarding(WD)
                if not isinstance(value,dict):raise ValueError('Expected a setup action')
                action=value.pop('action',None)
                fields={'github':{'role','owner','account_type'},'refresh':set(),'repository':{'repo_id'},
                        'check':{'revision'},'start':{'revision'},'reset':{'generation'}}
                if action not in fields or set(value)!=fields[action]:raise ValueError('Invalid setup action')
                result={}
                if action=='github':result=setup.begin(**value,origin=self._auth_config['origin'] if self._auth_config else 'http://'+self._public_host(),browser=self._browser())
                elif action=='refresh':result=setup.refresh()
                elif action=='repository':setup.select(value['repo_id'])
                else:result={'request':setup.queue(action,**value)}
                self._send(json.dumps({'ok':True,**result}));return
            if (Path(WD)/'state/factory-paused.json').exists():
                raise ValueError('Factory reset is in progress')
            setup_state=Path(WD)/'state/onboarding/setup.json'
            if self.path == '/api/runtime' and setup_state.is_file() and json.loads(setup_state.read_text()).get('phase')!='active':
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
                from forge.factory.issue_actions import decide, IMMEDIATE
                from forge.factory.issues import IssueInventory
                if not isinstance(value,dict) or set(value)!={'key','action','revision','reason'}:
                    raise ValueError('Expected issue, action, current revision, and decision')
                identifier=decide(WD,**value)
                result=IssueInventory(WD).view(key=value['key']) if value['action'] in IMMEDIATE else {}
                self._send(json.dumps({'ok':True,'request':identifier,**result,'controls_token':self._action_token}));return
            if self.path == '/api/issues/environment':
                from forge.factory.issue_actions import request
                from forge.factory.environments import override
                if not isinstance(value,dict) or set(value)!={'key','revision','environment'}:
                    raise ValueError('Expected the issue, current revision, and environment selection.')
                selected=override(value['environment'])
                reason=('Supervisor chooses the environment for this issue' if selected['mode']=='automatic' else
                        'Use '+(', '.join(selected['services']) if selected['mode']=='full' else 'the standard environment')+' for this issue')
                identifier=request(WD,key=value['key'],revision=value['revision'],action='environment',reason=reason,environment=selected)
                self._send(json.dumps({'ok':True,'request':identifier}));return
            if self.path == '/api/issues/refresh':
                from forge.factory.issues import IssueInventory
                if value!={} and (not isinstance(value,dict) or set(value)!={'key'}):raise ValueError('Expected an empty refresh request or an issue key')
                if value:
                    from forge.factory.control import request_check
                    request_check(WD, value['key'])
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
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/check':
                from forge.repository_setup import RepositorySetup
                if not isinstance(value,dict) or set(value)!={'repo_id','revision'} or (value['repo_id'] is not None and type(value['repo_id']) is not int):
                    raise ValueError('Expected repository identity and current revision')
                RepositorySetup(settings_service()).start_check(value['repo_id'],value['revision'])
                self._send(json.dumps({'ok':True}));return
            if self.path in ('/api/repositories/enable', '/api/repositories/disable'):
                from forge.repository_setup import RepositorySetup
                if (not isinstance(value,dict) or set(value)!={'repo_id','revision'}
                        or type(value['repo_id']) is not int or value['repo_id'] <= 0
                        or not isinstance(value['revision'],str) or not value['revision']):
                    raise ValueError('Expected repository identity and current revision')
                setup=RepositorySetup(settings_service())
                action=setup.enable if self.path.endswith('/enable') else setup.disable
                result=action(value['repo_id'],value['revision'])
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/deployment':
                from forge.repository_setup import RepositorySetup
                fields={'confirm':{'domain'},'reject':{'reason'},'reread':set()}
                action=value.get('action') if isinstance(value,dict) else None
                if (action not in fields or set(value)!={'repo_id','revision','action'}|fields[action]
                        or type(value['repo_id']) is not int):
                    raise ValueError('Expected repository identity, current revision, and a deployment decision')
                result=RepositorySetup(settings_service()).deployment(**value)
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/repositories/environment':
                from forge.repository_environments import RepositoryEnvironments
                from forge.repository_setup import RepositorySetup
                if (not isinstance(value,dict) or set(value)!={'repo_id','revision','environment_revision','action'}
                        or type(value['repo_id']) is not int or value['action'] not in ('scan','confirm','automatic')
                        or not isinstance(value['revision'],str) or not isinstance(value['environment_revision'],str)):
                    raise ValueError('Expected repository, current requirements, and scan or confirm.')
                settings=settings_service()
                RepositoryEnvironments(settings).act(**value)
                result=RepositorySetup(settings).view(value['repo_id'])
                self._send(json.dumps({**result,'ok':True,'controls_token':self._action_token}));return
            if self.path == '/api/settings/sync':
                if value != {}:raise ValueError('Expected an empty sync request')
                settings_service().start_refresh()
                self._send(json.dumps({'ok':True,'message':'Access refresh requested'}));return
            raise ValueError('invalid_action_request')
        except (ValueError,TypeError) as exc:
            self._send(json.dumps({'ok':False,'reason':str(exc)}))
        except (OSError,ImportError,subprocess.SubprocessError):
            self._send(json.dumps({'ok':False,'reason':'Operation unavailable; check server configuration'}))

    def do_GET(self):
        if not self._trusted_host() or not self._upstream():
            self.send_error(403);return
        u = urlparse(self.path)
        if not self._operator_guard():
            return
        if self._auth_get(u):
            return
        if not self._signed_in():
            return
        if u.path == '/api/connections':
            try:
                from forge.client_connections import ClientConnections
                result = ClientConnections(WD, auth=self._auth).view(self._session_token) if self._auth_config else {
                    'enabled': False, 'connections': [], 'reason': 'Configure HTTPS and operator sign-in to connect an agent.'}
                self._send(json.dumps({'ok': True, **result, 'controls_token': self._action_token}))
            except (ValueError, OSError, RuntimeError, KeyError, TypeError):
                self._send(json.dumps({'ok': False, 'reason': 'Connections are unavailable until setup or reset has completed.'}))
            return
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
                self._send(json.dumps({'ok':True,**Onboarding(WD).view(),'controls_token':self._action_token}))
            except (ValueError,OSError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Setup state is unavailable; inspect the supervisor service'}))
        elif u.path == '/api/issues/discussion':
            from forge.factory.discussions import Discussions
            try:
                query = parse_qs(u.query)
                if set(query) - {'issue', 'before'} or 'issue' not in query or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Invalid conversation view')
                result = Discussions(WD).view(query['issue'][0], int(query['before'][0]) if 'before' in query else None)
                self._send(json.dumps({'ok': True, **result, 'controls_token': self._action_token}))
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
                args=dict(repo_id=int(query['repo_id'][0]) if 'repo_id' in query else None,
                          state=query.get('state',['open'])[0],query=query.get('q',[''])[0],
                          page=int(query.get('page',['1'])[0]),key=query.get('issue',[None])[0])
                self._cached(('issues',)+tuple(sorted(args.items())), lambda: service.view(**args))
            except (OSError,ValueError,KeyError,RuntimeError):
                self._send(json.dumps({'ok':False,'reason':'Issue inventory is unavailable. Refresh to try again.'}))
        elif u.path == '/api/factory':
            try:
                if (Path(WD)/'state/factory-paused.json').exists():raise ValueError('Resetting')
                from forge.factory.control import view, select_view
                query=parse_qs(u.query, keep_blank_values=True)
                if set(query)-{'section','issue','limit','before'} or any(len(v)!=1 or not v[0] for v in query.values()):
                    raise ValueError('Invalid factory view')
                self._cached(('factory',)+tuple(sorted((k, v[0]) for k, v in query.items())),
                             lambda: select_view(VIEWS.get(WD,'factory',lambda:view(WD)), section=query.get('section',[None])[0], issue=query.get('issue',[None])[0],
                                                 limit=query.get('limit',[None])[0], before=query.get('before',[None])[0]))
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
                else:self._send(json.dumps({'ok':True,**service.view(repo_id),'controls_token':self._action_token}))
            except (OSError,ValueError,TypeError,KeyError):
                self._send(json.dumps({'ok':False,'reason':'Repository setup changed or is unavailable. Check again.'}))
        elif u.path == '/api/settings':
            try:
                if (Path(WD)/'state/factory-paused.json').exists():raise ValueError('Resetting')
                query=parse_qs(u.query)
                if query and query != {'summary':['1']}: raise ValueError('Invalid settings view')
                result=settings_service().view()
                if query:
                    fields=('id','name','enabled','configured','access','access_ready','protection_ready','protection_status','protection_previous_status','dispatch_allowed','setup_url')
                    result['repositories']=[{key:row[key] for key in fields if key in row} for row in result['repositories']]
                result.update(ok=True,controls_token=self._action_token)
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
    if not address.is_loopback:
        raise SystemExit('The status API binds loopback only; the operator UI serves the network.')
    UPSTREAM_SECRET = upstream_secret(os.environ.get('FORGE_UPSTREAM_SECRET_FILE') or Path(WD) / 'state/ui-upstream.secret')
    print(f"forge-status on http://{ip}:{port}")
    threading.Thread(target=settings_service().watch,daemon=True).start()
    ThreadingHTTPServer((ip, port), H).serve_forever()

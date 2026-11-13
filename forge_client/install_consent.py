"""Local single-App manifest receiver for the deterministic installation command.

Only App-owned credentials cross SSH. Manifest conversion never uses the local
GitHub administrator token. The private response checkpoint survives a lost SSH
reply and is deleted only after the host independently proves App identity.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import json
import secrets
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .contracts import SetupError, require, digest
from .connection import Vault
from .network import payload
from .workflow import Journal


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def convert(code):
    require(isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', code), 'invalid_callback')
    request = Request('https://api.github.com/app-manifests/' + code + '/conversions', data=b'', method='POST',
        headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                 'User-Agent': 'Forge-Installation-CLI'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(65537)
        require(len(raw) <= 65536, 'invalid_conversion')
        app = json.loads(raw)
        require(isinstance(app, dict), 'invalid_conversion')
        return {key: app[key] for key in ('id', 'slug', 'pem', 'client_id', 'client_secret')}
    except Exception:
        raise SetupError('conversion_unconfirmed', 'GitHub App conversion was not confirmed; inspect the registered App before continuing.') from None


def event(value):
    print(json.dumps(value, allow_nan=False), file=sys.stderr, flush=True)


class InstallConsent:
    def __init__(self, installation, selected, *, github, journal=None, convert_app=convert,
                 tell=event, open_browser=None, wait=600, vault=None, repositories=None):
        self.installation, self.selected, self.github = installation, selected, github
        self.host_state = installation.journal.read()
        require(self.host_state and self.host_state.get('origin'), 'network_required')
        self.installation.host.fingerprint = self.host_state.get('host_key')
        self.actor = github.identity()
        require(self.actor['id'] in [row['id'] for row in selected['operators']], 'operator_required',
                'Sign in locally with one of the installation operators.')
        self.binding = {'setup_id': self.host_state['intent']['id'], 'owner': selected['owner'],
                        'operators': sorted({row['id'] for row in selected['operators']}), 'origin': self.host_state['origin'],
                        'workflow_edits': any(row['allow_workflow_edits'] for row in selected['repositories']),
                        'repositories': [{'id': row['id'], 'name': row['name']} for row in
                            (repositories if repositories is not None else [row['target'] for row in selected['repositories']])]}
        self.journal = journal or Journal('install-consent', installation.ssh, root=installation.journal.root)
        self.state = self.journal.read() or {'version': 1, 'binding': self.binding, 'phase': 'new'}
        require(self.state['binding'] == self.binding, 'consent_changed', 'Saved consent belongs to another installation.')
        self.vault = vault or Vault()
        self.secret_key = 'install-app-' + digest(self.binding)
        self.convert_app, self.tell, self.open_browser, self.wait = convert_app, tell, open_browser, wait
        require(type(wait) is int and 0 <= wait <= 3600)

    def save(self):
        self.journal.save(self.state)

    def call(self, action, **parameters):
        response = self.installation.host.rpc(payload('_factory_host.py'), {
            'version': 1, 'intent': self.host_state['intent'], 'setup': {
                'version': 1, 'setup_id': self.binding['setup_id'], 'actor_id': self.actor['id'], 'generation': None,
                'action': 'consent', 'parameters': {'action': action, 'binding': self.binding, 'parameters': parameters}}}, timeout=270)
        require(isinstance(response, dict) and response.get('ok') is True and isinstance(response.get('result'), dict),
                'consent_host_failed', 'The host could not complete App consent; resume to inspect the saved state.')
        return response['result']

    def observe(self):
        result = self.call('status')
        if result['status'] in ('consent_ready', 'app_installation_required'):
            return result
        if self.state['phase'] == 'converting' and self.vault.get(self.secret_key) is None:
            return {'status': 'write_unconfirmed', 'reason': 'App conversion may have succeeded. Inspect the existing GitHub App; this command will not register a duplicate.',
                    'url': 'https://github.com/settings/apps' if self.binding['owner']['type'] == 'User' else
                    'https://github.com/organizations/' + self.binding['owner']['login'] + '/settings/apps'}
        return result

    def checkpoint(self, app):
        require(len(json.dumps(app, allow_nan=False).encode()) <= 65536, 'invalid_conversion')
        require(self.vault.get(self.secret_key) is None, 'app_changed')
        self.vault.set(self.secret_key, app)

    def import_checkpoint(self):
        app = self.vault.get(self.secret_key)
        require(isinstance(app, dict), 'invalid_storage')
        result = self.call('import', app=app)
        require(result.get('app_id') == app['id'] and result.get('identity_ready'), 'consent_host_failed')
        self.state['phase'] = 'registered'; self.state['app_id'] = app['id']; self.save()
        self.vault.delete(self.secret_key)
        return result

    def installation_url(self, url):
        return url + '?' + urlencode([('suggested_target_id', self.binding['owner']['id']),
            *[('repository_ids[]', row['id']) for row in self.binding['repositories']]])

    def installation_instruction(self):
        names = ', '.join(row['name'] for row in self.binding['repositories'])
        return ('On GitHub’s installation page, choose “Only select repositories” and select exactly: '
                + names + '. Check the selection before clicking Install; GitHub may default to “All repositories”.')

    def installation_guidance(self):
        return '<p>' + html.escape(self.installation_instruction()) + '</p>'

    def action(self, url, instruction):
        self.tell({'status': 'browser_action_required', 'url': url,
                   'instruction': instruction + ' ' + self.installation_instruction(),
                   'repository_selection': 'selected',
                   'repositories': [dict(row) for row in self.binding['repositories']]})
        if self.open_browser:
            self.open_browser(url)

    def apply(self, observed=None):
        result = self.observe()
        if result['status'] == 'consent_ready':
            # A lost import reply may leave a private checkpoint. Observe proves
            # completion but only import verifies its precise App before deletion.
            if self.vault.get(self.secret_key) is not None: self.import_checkpoint()
            return result
        if self.vault.get(self.secret_key) is not None:
            result = self.import_checkpoint()
        if result['status'] == 'write_unconfirmed': return result
        if result['status'] == 'app_installation_required':
            result = self.call('refresh')
            if result['status'] != 'consent_ready':
                self.action(self.installation_url(result['install_url']), 'Install the Forge App on the selected repositories, then resume forge install.')
            return result
        return self.receive()

    def receive(self):
        consent = self
        outcome = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, status, body, location=None, nonce=None):
                encoded = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                # Preserve the loopback Origin on local form POSTs while keeping
                # the private setup URL out of cross-origin GitHub requests.
                self.send_header('Referrer-Policy', 'same-origin')
                self.send_header('Content-Security-Policy', "default-src 'none'; form-action 'self' https://github.com; frame-ancestors 'none'"
                                 + ("; script-src 'nonce-" + nonce + "'" if nonce else ''))
                if location: self.send_header('Location', location)
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers(); self.wfile.write(encoded)
            def do_GET(self):
                if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                    self.reply(400, 'Invalid host'); return
                parsed = urlsplit(self.path)
                if parsed.path == '/start/' + consent.state['browser'] and not parsed.query:
                    if consent.state['phase'] != 'waiting':
                        body = ('<!doctype html><title>Continue Forge setup</title><p>The manifest was sent to GitHub. '
                                'Finish the existing GitHub registration tab.</p>' + consent.installation_guidance())
                        if consent.state['phase'] == 'submitted':
                            consent.state['refusal_nonce'] = secrets.token_urlsafe(24); consent.save()
                            body += ('<h1>Did GitHub report a missing manifest?</h1><p>Use this recovery only if GitHub displayed '
                                '“We didn’t find an App Manifest for your request” and no App was created. Finish the GitHub '
                                'passkey check first. This resends the same registration; keep its App name unchanged.</p>'
                                '<form method="post" action="/retry/' + consent.state['browser'] + '">'
                                '<input type="hidden" name="nonce" value="' + consent.state['refusal_nonce'] + '">'
                                '<label><input type="checkbox" name="missing_manifest_no_app" value="confirmed" required>'
                                'GitHub reported a missing manifest and no App was created.</label>'
                                '<button type="submit">Resend this manifest</button></form>')
                        self.reply(200, body); return
                    body = ('<!doctype html><title>Set up Forge</title><h1>Register your Forge App</h1>'
                        '<p>Approve the App on GitHub, then install it for this factory.</p>'
                        + consent.installation_guidance() +
                        '<form method="post" action="/launch/' + consent.state['browser']
                        + '"><button type="submit">Continue on GitHub</button></form>')
                    self.reply(200, body); return
                query = parse_qs(parsed.query)
                expected = parse_qs(urlsplit(consent.state['registration']['action']).query)['state']
                if (parsed.path != '/callback' or set(query) != {'state', 'code'} or query['state'] != expected
                        or len(query['code']) != 1 or consent.state['phase'] != 'submitted'):
                    self.reply(400, 'This callback is invalid or already consumed. Resume the CLI.'); return
                consent.state['phase'] = 'converting'; consent.save()
                try:
                    app = consent.convert_app(query['code'][0])
                    consent.checkpoint(app)
                    result = consent.import_checkpoint()
                    outcome.append(result)
                    self.reply(303, 'App registered. Continue with GitHub installation.', consent.installation_url(result['install_url']))
                except Exception:
                    outcome.append({'status': 'write_unconfirmed', 'reason': 'App registration needs reconciliation. Resume the CLI; it will preserve the existing App.'})
                    self.reply(503, 'Registration was interrupted. Return to the CLI and resume; do not create another App.')
            def do_POST(self):
                origin = 'http://127.0.0.1:' + str(self.server.server_port)
                if (self.headers.get('Host') != urlsplit(origin).netloc
                        or self.headers.get('Origin', origin) != origin):
                    self.reply(400, 'Invalid origin'); return
                retry = self.path == '/retry/' + consent.state['browser']
                if retry:
                    if consent.state['phase'] != 'submitted':
                        self.reply(409, 'This registration cannot be resent after conversion. Resume the CLI.'); return
                    length = self.headers.get('Content-Length', '')
                    if not length.isdigit() or not 1 <= int(length) <= 512:
                        self.reply(400, 'Confirm the exact GitHub refusal first.'); return
                    fields = parse_qs(self.rfile.read(int(length)).decode('ascii', errors='replace'))
                    if (set(fields) != {'nonce', 'missing_manifest_no_app'}
                            or fields['missing_manifest_no_app'] != ['confirmed']
                            or fields['nonce'] != [consent.state.get('refusal_nonce')]):
                        self.reply(409, 'Confirm the missing-manifest refusal using the current recovery form.'); return
                    consent.state.pop('refusal_nonce', None)
                    consent.state['reported_manifest_refusals'] = consent.state.get('reported_manifest_refusals', 0) + 1
                elif self.path != '/launch/' + consent.state['browser'] or consent.state['phase'] != 'waiting':
                    self.reply(409, 'Registration already submitted or invalid. Finish the existing GitHub tab.'); return
                # Persist BEFORE handing a mutating form to the browser. A lost
                # response must never lead to another manifest registration.
                consent.state['phase'] = 'submitted'; consent.save()
                registration = consent.state['registration']
                nonce = secrets.token_urlsafe(24)
                body = ('<!doctype html><title>Continue to GitHub</title><form id="github" method="post" action="'
                    + html.escape(registration['action'], quote=True)
                    + '"><input type="hidden" name="manifest" value="'
                    + html.escape(json.dumps(registration['manifest']), quote=True)
                    + '"></form><script nonce="' + nonce + '">document.getElementById("github").submit()</script>')
                self.reply(200, body, nonce=nonce)
        port = self.state.get('port', 0)
        try:
            server = HTTPServer(('127.0.0.1', port), Handler)
        except OSError:
            raise SetupError('receiver_unavailable', 'The saved local callback port is busy; close the previous installer and resume.') from None
        with server:
            server.timeout = 0.5
            self.state.setdefault('port', server.server_port)
            self.state.setdefault('browser', secrets.token_urlsafe(32))
            self.save()
            registration = self.call('begin', callback='http://127.0.0.1:' + str(server.server_port) + '/callback')
            if registration['status'] != 'consent_required': return registration
            self.state.update(registration=registration['registration'])
            if self.state['phase'] == 'new': self.state['phase'] = 'waiting'
            self.save()
            url = 'http://127.0.0.1:' + str(server.server_port) + '/start/' + self.state['browser']
            instruction = ('Keep this command running. Finish the existing GitHub registration tab. If GitHub explicitly reported '
                           'a missing manifest and no App was created, open this local recovery page after the passkey check.'
                           if self.state['phase'] == 'submitted' else
                           'Keep this command running. Open this URL on this computer, approve the Forge App, and install it on the selected repositories.')
            self.action(url, instruction)
            deadline = time.monotonic() + self.wait
            while not outcome and time.monotonic() < deadline:
                server.handle_request()
            if outcome: return outcome[0]
            return {'status': 'consent_required', 'registration_submitted': self.state['phase'] == 'submitted',
                    'reason': 'Browser consent timed out. Resume the same installation to reopen its callback receiver and finish the existing GitHub tab.'}

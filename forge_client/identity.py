"""Fresh-host adapter over the existing identity bootstrap; no new sign-in logic."""
from types import SimpleNamespace
import webbrowser

from . import bootstrap
from .connection import connection_lock
from .contracts import require
from .install import Installation, ROOT, SOURCE
from .network import payload


class IdentityHost:
    def __init__(self, workflow, intent):
        self.workflow, self.intent = workflow, intent

    def call(self, action, target, revision=None, secret=None):
        result = self.workflow.host.rpc(payload('_identity_host.py'), {
            'version': 1, 'intent': self.intent,
            'bootstrap': {'version': 1, 'action': action, 'target': target, 'revision': revision, 'secret': secret}}, timeout=450)
        return bootstrap.Host.validate(result, target)


def execute(args, *, workflow=None, setup=bootstrap.setup):
    workflow = workflow or Installation(args.ssh)
    with connection_lock(workflow.key, workflow.journal.root):
        state = workflow.journal.read()
        require(state and state.get('ssh') == workflow.ssh, 'setup_not_found', 'Start with forge setup new for this host.')
        require(state.get('origin'), 'network_not_ready', 'Complete forge setup network before configuring operator sign-in.')
        operators = args.operator or state.get('operators')
        require(operators, 'operator_required', 'Supply --operator with the GitHub login to authorize.')
        workflow.host.fingerprint = state.get('host_key')
        host = IdentityHost(workflow, state['intent'])
        request = SimpleNamespace(ssh=workflow.ssh, source=SOURCE, root=ROOT, origin=state['origin'],
                                  operator=operators, client_id=args.client_id or state.get('identity_client_id'),
                                  apply=args.apply, resume=args.resume, secret_stdin=args.secret_stdin, json=args.json)
        result = setup(request, host_factory=lambda *a: host)
        state['operators'] = operators
        if result.get('client_id') or request.client_id:
            state['identity_client_id'] = result.get('client_id') or request.client_id
        if result['status'] == 'sign_in_required':
            state['milestones'].setdefault('https_configured', workflow.clock())
            result['next_action']['instruction'] = 'Continue with GitHub to verify your operator account. Role Apps, coding accounts, repository activation and first-merge validation are still required.'
        if result['status'] == 'identity_app_required':
            result['next_action']['application_name'] = state['intent']['name'] + ' — Forge operator sign-in'
        workflow.journal.save(state)
        return result


def wizard(workflow, *, ask=input, tell=print, open_browser=webbrowser.open):
    state = workflow.journal.read()
    operators = state.get('operators') or [ask('GitHub operator login: ').strip()]
    args = SimpleNamespace(ssh=workflow.ssh, operator=operators, client_id=None,
                           apply=False, resume=False, secret_stdin=False, json=False)
    result = execute(args, workflow=workflow)
    if result['status'] == 'identity_app_required':
        action = result['next_action']
        tell('Create a dedicated identity-only GitHub OAuth App:')
        for key in ('application_name', 'homepage_url', 'callback_url'):
            tell('  ' + key.replace('_', ' ') + ': ' + action[key])
        open_browser(action['url'])
        args.client_id = ask('OAuth App client ID (public): ').strip()
        result = execute(args, workflow=workflow)
    if result['status'] == 'planned':
        if ask('Configure operator sign-in and private HTTPS? [y/N] ').strip().lower() not in ('y', 'yes'):
            return result
        args.apply = True
        # Existing bootstrap owns the hidden secret prompt and validates input.
        result = execute(args, workflow=workflow)
    if result['status'] == 'sign_in_required':
        tell(result['next_action']['instruction'])
        open_browser(result['next_action']['url'])
    return result

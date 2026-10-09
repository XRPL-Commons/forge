"""Fresh-host adapter over the existing identity bootstrap; no new sign-in logic."""
from types import SimpleNamespace
import shlex
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


def registration(result, *, tell=print):
    action = result['next_action']
    tell('GitHub sign-in needs a dedicated OAuth App. The CLI provides its settings; you register it on GitHub.')
    tell('1. Open ' + action['url'])
    tell('2. Enter these fields exactly:')
    for key, label in (('application_name', 'Application name'), ('homepage_url', 'Homepage URL'), ('callback_url', 'Redirect URI / Authorization callback URL')):
        tell('   ' + label + ': ' + action[key])
    tell('   Leave wildcard matching and Device Flow off. This sign-in flow requests no repository or admin scopes.')
    tell('3. Click Register application, then copy the Client ID from the App settings page.')
    tell('   Keep that page open. The next step explains how to generate and enter the client secret.')


def display(result, ssh, *, tell=print):
    """Human output for the same structured sign-in results used by agents."""
    status = result.get('status')
    if status not in ('identity_app_required', 'identity_secret_required', 'planned', 'sign_in_required'):
        return False
    # Other setup stages also use "planned".
    if status == 'planned' and 'client_id' not in result:
        return False
    command = 'forge setup sign-in' + (' --ssh ' + shlex.quote(ssh) if ssh else '')
    if status == 'identity_app_required':
        registration(result, tell=tell)
        tell('\nContinue the guided setup: ' + command)
    elif status == 'planned':
        tell('GitHub sign-in setup for ' + result['server'])
        tell('Client ID: ' + result['client_id'])
        for step in result['steps']:
            tell('  • ' + step)
        tell('\nContinue the guided setup: ' + command)
    elif status == 'identity_secret_required':
        tell('The GitHub App client secret is still needed. Use the hidden prompt: ' + command)
    else:
        tell('Sign-in configuration and HTTPS checks passed.')
        tell('Open ' + result['next_action']['url'] + ' and choose Continue with GitHub.')
        tell('Sign in with the authorized account: ' + ', '.join(o['login'] for o in result['operators']) + '.')
        tell('Role Apps, coding accounts, repository activation and first-merge validation are still required.')
    return True


def display_error(error, ssh, *, tell=print):
    tell('Setup could not finish: ' + str(error))
    if error.code in ('https_unavailable', 'https_setup_failed', 'host_update_failed'):
        tell('Your sign-in settings are saved. You do not need to enter the secret again.')
        tell('Retry the remaining checks: forge setup sign-in --ssh ' + shlex.quote(ssh) + ' --resume')


def wizard(workflow, *, ask=input, tell=print, open_browser=webbrowser.open, operator=None, client_id=None):
    tell('\nSet up GitHub sign-in')
    tell('Progress is saved. You can close this wizard and run forge setup sign-in --ssh ' + workflow.ssh + ' to continue.')
    state = workflow.journal.read()
    require(state, 'setup_not_found', 'Start with forge setup new --ssh ' + workflow.ssh + '.')
    operators = operator or state.get('operators') or [ask('GitHub username allowed to sign in: ').strip()]
    args = SimpleNamespace(ssh=workflow.ssh, operator=operators, client_id=client_id,
                           apply=False, resume=False, secret_stdin=False, json=False)
    result = execute(args, workflow=workflow)
    if result['status'] == 'identity_app_required':
        action = result['next_action']
        registration(result, tell=tell)
        open_browser(action['url'])
        args.client_id = ask('OAuth App client ID (public): ').strip()
        result = execute(args, workflow=workflow)
    if result['status'] == 'planned':
        tell('\nSelected server: ' + result['server'])
        tell('Allowed GitHub account(s): ' + ', '.join(o['login'] for o in result['operators']))
        tell('The CLI will configure sign-in, update the services, and verify private HTTPS.')
        if ask('Continue with these settings? [y/N] ').strip().lower() not in ('y', 'yes'):
            return result
        args.apply = True
        # Existing bootstrap owns the hidden secret prompt and validates input.
        result = execute(args, workflow=workflow)
    if result['status'] == 'sign_in_required':
        open_browser(result['next_action']['url'])
    return result

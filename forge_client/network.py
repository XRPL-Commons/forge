"""Private-network stage shared by the setup wizard and noninteractive callers."""
from importlib.resources import files
import re
import time
import webbrowser

from .connection import connection_lock
from .contracts import require
from .install import Installation, ERRORS


def payload(component='_network_host.py'):
    require(component in ('_network_host.py', '_identity_host.py'))
    helpers = files('forge_client').joinpath('_install_host.py').read_text()
    network = files('forge_client').joinpath(component).read_text()
    return ('import sys, types\n_helpers=types.ModuleType("forge_install_helpers")\n'
            'exec(compile(' + repr(helpers) + ',"<forge-helpers>","exec"),_helpers.__dict__)\n'
            'sys.modules["forge_install_helpers"]=_helpers\n' + network)


class Network:
    def __init__(self, installation):
        self.installation = installation

    def execute(self, *, apply=False, hostname=None, wait=0, upgrade_installer=False):
        workflow = self.installation
        require(type(wait) is int and 0 <= wait <= 600, 'invalid_wait', 'Use --wait between 0 and 600 seconds.')
        require(type(apply) is bool and type(upgrade_installer) is bool and (not upgrade_installer or apply),
                'invalid_request', '--upgrade-installer requires --apply for network setup.')
        with connection_lock(workflow.key, workflow.journal.root):
            state = workflow.journal.read()
            require(state and state.get('ssh') == workflow.ssh, 'setup_not_found', 'Start with forge setup new for this host.')
            saved = state.get('network_hostname')
            chosen = hostname or saved or re.sub('[^a-z0-9-]+', '-', state['intent']['name'].lower()).strip('-')[:63]
            require(re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', chosen),
                    'invalid_hostname', 'Choose --hostname with 1–63 lowercase letters, digits and hyphens.')
            require(saved is None or saved == chosen, 'setup_conflict', 'Resume network setup with its saved hostname.')
            workflow.host.fingerprint = state.get('host_key')
            program = payload()
            request = {'version': 1, 'action': 'apply' if apply else 'plan', 'intent': state['intent'],
                       'hostname': chosen, 'upgrade_installer': upgrade_installer}
            result = self.validate(workflow.host.rpc(program, request))
            state['network_hostname'] = chosen
            workflow.journal.save(state)
            deadline = time.monotonic() + wait
            while result['status'] == 'network_installing' and time.monotonic() < deadline:
                workflow.sleep(min(2, max(0, deadline - time.monotonic())))
                result = self.validate(workflow.host.rpc(program, {**request, 'action': 'plan', 'upgrade_installer': False}))
            if result['status'] == 'network_ready':
                state['origin'] = result['origin']
                state['milestones'].setdefault('tailnet_connected', workflow.clock())
                workflow.journal.save(state)
            instruction = {
                'network_required': 'Run forge setup network with --apply to install Tailscale and request private network access.',
                'network_installing': 'Network setup continues on the host. Run forge setup network again to check progress.',
                'tailnet_authorization_required': 'Open the browser link and approve this VM in your Tailscale account, then rerun forge setup network.',
                'tailnet_approval_required': 'Approve this VM in the Tailscale admin console, then rerun forge setup network.',
                'network_interrupted': ERRORS.get(result['error'], 'Resume network setup with --apply.'),
                'network_ready': 'The VM is on the private tailnet. Configure operator sign-in before exposing the Forge UI over HTTPS.',
            }[result['status']]
            return {**result, 'ssh': workflow.ssh, 'hostname': chosen,
                    'setup_id': state['intent']['id'], 'next_action': {'instruction': instruction, 'url': result['url']}}

    @staticmethod
    def validate(result):
        require(isinstance(result, dict) and set(result) == {'ok', 'status', 'origin', 'url', 'step', 'error'}
                and result['status'] in ('network_required', 'network_installing', 'tailnet_authorization_required',
                                         'tailnet_approval_required', 'network_interrupted', 'network_ready')
                and result['step'] in (None, 'packages', 'authorization', 'complete')
                and result['error'] in (None, *ERRORS), 'invalid_host_response', 'Invalid private network response.')
        require(result['origin'] is None or isinstance(result['origin'], str)
                and re.fullmatch(r'https://[a-z0-9][a-z0-9-]*\.[a-z0-9.-]+\.ts\.net', result['origin']),
                'invalid_host_response', 'Invalid private network origin.')
        require(result['url'] is None or isinstance(result['url'], str) and
                (re.fullmatch(r'https://login\.tailscale\.com/a/[A-Za-z0-9]+', result['url'])
                 or result['url'] == 'https://login.tailscale.com/admin/machines'),
                'invalid_host_response', 'Invalid network authorization link.')
        require(result['status'] != 'network_ready' or result['origin'] is not None,
                'invalid_host_response', 'The host did not confirm a private network address.')
        require(result['status'] not in ('tailnet_authorization_required', 'tailnet_approval_required') or result['url'] is not None,
                'invalid_host_response', 'The host did not return the required approval link.')
        return result


def execute(args):
    return Network(Installation(args.ssh)).execute(apply=args.apply, hostname=args.hostname,
                                                  wait=args.wait, upgrade_installer=args.upgrade_installer)


def wizard(workflow, *, ask=input, tell=print, open_browser=webbrowser.open, identity_wizard=None):
    network = Network(workflow)
    result = network.execute()
    if result['status'] in ('network_required', 'network_interrupted'):
        tell('Next: install Tailscale for private access. The Forge UI stays on loopback until sign-in is configured.')
        if ask('Set up private network access? [y/N] ').strip().lower() not in ('y', 'yes'):
            return result
        result = network.execute(apply=True)
    try:
        while result['status'] == 'network_installing':
            tell('Configuring private network access…')
            result = network.execute(wait=10)
        if result['status'] in ('tailnet_authorization_required', 'tailnet_approval_required'):
            tell(result['next_action']['instruction'] + '\n' + result['url'])
            open_browser(result['url'])
            ask('Press Enter after approving the VM, or Ctrl+C to resume later. ')
            result = network.execute()
    except KeyboardInterrupt:
        tell('Network setup is saved. Resume with forge setup new --ssh ' + workflow.ssh + '.')
        return result
    tell(result['next_action']['instruction'])
    if result['status'] == 'network_ready':
        if identity_wizard is None:
            from .identity import wizard as identity_wizard
        return identity_wizard(workflow, ask=ask, tell=tell)
    return result

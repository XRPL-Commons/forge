"""Observe, provision and resume a qualified database worker through verified SSH."""
from importlib.resources import files
import re

from .contracts import require


def payload():
    helpers = files('forge_client').joinpath('_install_host.py').read_text()
    component = files('forge_client').joinpath('_environment_host.py').read_text()
    return ('import sys, types\n_helpers=types.ModuleType("forge_install_helpers")\n'
            'exec(compile(' + repr(helpers) + ',"<forge-helpers>","exec"),_helpers.__dict__)\n'
            'sys.modules["forge_install_helpers"]=_helpers\n' + component)


class Environment:
    def __init__(self, installation):
        self.installation = installation

    def execute(self, *, apply=False):
        workflow = self.installation
        require(type(apply) is bool)
        state = workflow.journal.read()
        require(state and state.get('ssh') == workflow.ssh, 'setup_not_found', 'Start forge install for this host first.')
        workflow.host.fingerprint = state.get('host_key')
        reply = workflow.host.rpc(payload(), {'version': 1, 'intent': state['intent'], 'apply': apply}, timeout=90)
        result = reply.get('result')
        require(isinstance(result, dict) and result.get('status') in
                ('environment_required', 'environment_installing', 'environment_interrupted', 'environment_ready'),
                'invalid_host_response')
        require(result.get('tier') == 'full' and result.get('services') == ['mongodb', 'postgres', 'redis']
                and (result.get('identity') or {}).get('setup_id') == state['intent']['id'], 'invalid_host_response')
        if result['status'] == 'environment_ready':
            require(isinstance(result.get('image'), str) and re.fullmatch('sha256:[a-f0-9]{64}', result['image'])
                    and (result.get('receipt') or {}).get('status') == 'complete'
                    and result['receipt'].get('qualified_at') is not None, 'invalid_host_response')
        return result

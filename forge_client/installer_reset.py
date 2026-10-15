"""Explicit plan/apply for a clean Forge reinstall on an existing Ubuntu VM."""
from importlib.resources import files
import re

from .connection import connection_lock
from .contracts import digest, require
from .install import Installation
from .workflow import Journal


def payload():
    helpers = files('forge_client').joinpath('_install_host.py').read_text()
    component = files('forge_client').joinpath('_reset_host.py').read_text()
    return ('import sys, types\n_helpers=types.ModuleType("forge_install_helpers")\n'
            'exec(compile(' + repr(helpers) + ',"<forge-helpers>","exec"),_helpers.__dict__)\n'
            'sys.modules["forge_install_helpers"]=_helpers\n' + component)


class Reset:
    def __init__(self, installation, *, journal=None):
        self.installation = installation
        self.journal = journal or Journal('installation-reset', installation.ssh, root=installation.journal.root)

    def execute(self, *, apply=False, revision=None):
        require(type(apply) is bool and (not apply or isinstance(revision, str)
                and re.fullmatch('[a-f0-9]{64}', revision)), 'reset_revision_required',
                'Inspect forge install --reset, then supply --reset --apply --reset-revision with that plan revision.')
        workflow = self.installation
        with connection_lock(workflow.key, workflow.journal.root):
            saved = self.journal.read()
            state = workflow.journal.read()
            if state is None:
                require(saved and saved.get('host_state'), 'setup_not_found', 'No saved installation exists for this host.')
                state = saved['host_state']
            require(state.get('ssh') == workflow.ssh, 'setup_changed')
            if saved and saved['host_state']['intent'] != state['intent']:
                require(not apply, 'setup_changed', 'A new installation exists; inspect its own reset plan first.')
                saved = None
            if apply:
                require(saved and saved['plan']['revision'] == revision, 'reset_revision_required',
                        'Read the current reset plan before applying it.')
            workflow.host.fingerprint = state.get('host_key')
            response = workflow.host.rpc(payload(), {'version': 1, 'intent': state['intent'],
                'apply': apply, 'revision': revision}, timeout=180)
            result = response.get('result')
            require(isinstance(result, dict) and result.get('status') in
                    ('reset_planned', 'reset_pending', 'reset_complete'), 'invalid_host_response')
            identity = result.get('identity') or {}
            require(identity.get('intent') == state['intent'] and result.get('revision') == digest(identity)
                    and (not apply or result['revision'] == revision), 'invalid_host_response')
            if not saved:
                install_journal = Journal('installation', workflow.ssh, root=workflow.journal.root)
                consent = Journal('install-consent', workflow.ssh, root=workflow.journal.root).read()
                require(consent is None or consent.get('binding', {}).get('setup_id') == state['intent']['id'],
                        'setup_changed', 'The local App consent belongs to another installation.')
                saved = {'version': 1, 'host_state': state, 'installation_state': install_journal.read(),
                         'consent_state': consent, 'plan': result}
            else:
                saved['plan'] = result
            self.journal.save(saved)
            if result['status'] == 'reset_complete':
                self.archive(saved)
            return {**result, 'ssh': workflow.ssh,
                    'next_action': ['forge', 'install', '--ssh', workflow.ssh] if result['status'] == 'reset_complete'
                        else ['forge', 'install', '--ssh', workflow.ssh, '--reset', '--apply', '--reset-revision', result['revision']]}

    def archive(self, saved):
        workflow = self.installation
        for name, journal, state in (
            ('fresh-host', workflow.journal, saved['host_state']),
            ('installation', Journal('installation', workflow.ssh, root=workflow.journal.root), saved['installation_state']),
            ('install-consent', Journal('install-consent', workflow.ssh, root=workflow.journal.root), saved.get('consent_state'))):
            if state is None:
                continue
            current = journal.read()
            require(current is None or current == state, 'setup_changed',
                    'Local installation state changed during reset; the new journal was preserved.')
            archive = Journal('reset-archive-' + name, saved['host_state']['intent']['id'], root=workflow.journal.root)
            previous = archive.read()
            require(previous is None or previous == state, 'setup_changed')
            archive.save(state)
            if current is not None:
                journal.path.unlink()


def execute(args):
    return Reset(Installation(args.ssh)).execute(apply=args.apply, revision=args.reset_revision)

"""Drive the ordinary factory pipeline, then verify its exact GitHub merge."""
from datetime import datetime

from .contracts import require
from .install_targets import prepare_targets


class InstallSmoke:
    def __init__(self, factory, install_state, save):
        self.factory, self.state, self.save = factory, install_state, save
        self.github = factory.github

    def prepare(self, *, apply=False):
        targets = prepare_targets(self.state, self.save, self.github, apply=apply)
        if targets['status'] != 'targets_ready':
            return targets
        target = targets['smoke']
        saved = self.state.get('smoke')
        if saved:
            require(saved['repository'] == target, 'smoke_changed')
            return {'status': 'smoke_repository_ready', **saved}
        if not apply:
            return {'status': 'smoke_pending', 'repository': target, 'reason': 'Apply prepares the smoke repository and its issue.'}
        selected = next((row for row in self.state['intent']['repositories'] if row['target']['id'] == target['id']), None)
        mode = selected.get('github_protection') if selected else ('optional' if all(row.get('github_protection') == 'optional' for row in self.state['intent']['repositories']) else None)
        prepared = self.factory.prepare_repository(target['name'], apply=True, initial=False,
                                                   repair=self.state['intent']['rules_repair'], github_protection=mode)
        if prepared['status'] != 'repository_ready':
            return prepared
        response = self.factory.call('smoke_issue', intent_digest=self.state['inputs_digest'], repository=target,
                                     number=self.state['intent'].get('smoke_issue'), started_at=self.state['started_at'])
        if response['status'] != 'smoke_issue_ready':
            return response
        selected = next((row for row in self.state['intent']['repositories'] if row['target']['id'] == target['id']), None)
        saved = {'repository': target, 'number': response['number'],
                 'smoke_policy': selected['merge_policy'] if selected else 'auto'}
        self.state['smoke'] = saved
        self.save()
        return {'status': 'smoke_repository_ready', **saved}

    def run(self, *, apply=False, retry_proposal=None):
        prepared = self.prepare(apply=apply)
        if prepared['status'] != 'smoke_repository_ready':
            return prepared
        saved = self.state['smoke']
        if not apply:
            return self.factory.call('smoke_status')
        if retry_proposal:
            retried = self.factory.call('smoke_retry', proposal_id=retry_proposal)
            if retried['status'] != 'smoke_retry_queued':
                return retried
        binding = {**saved, 'intent_digest': self.state['inputs_digest'], 'started_at': self.state['started_at'],
                   'repositories': [{**row, 'target': {key: row['target'][key] for key in ('id', 'name')}}
                                    for row in self.state['intent']['repositories']]}
        bound = self.factory.call('smoke_bind', binding=binding)
        if bound.get('phase') == 'controller':
            return bound
        view = self.factory.call('status')
        if view['phase'] == 'setup':
            if view.get('selected') != saved['repository']:
                view = self.factory.call('select', repository=saved['repository'])
            view = self.factory.check(start=bool(view.get('can_start')), apply=True, wait=0)
            if view['phase'] != 'active':
                return {'status': 'smoke_pending', 'phase': 'verification' if not view.get('can_start') else 'starting',
                        'checks': view.get('checks'), 'operation': view.get('operation'), 'error': view.get('error')}
        result = self.factory.call('smoke_launch')
        return self.verify(result)

    def verify(self, result):
        if result['status'] == 'handover_complete':
            return result
        if result['status'] != 'smoke_merged':
            return result
        saved = self.state['smoke']
        candidate = result['candidate']
        remote = self.github.merged_pull(saved['repository'], candidate['pull_number'])
        issue = self.github.pilot_issue(saved['repository'], saved['number'])
        require(remote['merged'] and remote['head_sha'] == candidate['head_sha']
                and remote['merge_sha'] == candidate['merge_sha'] and issue['state'] == 'closed',
                'merge_unverified', 'GitHub has not confirmed the exact smoke merge and closed issue.')
        try:
            merged_at = datetime.fromisoformat(remote['merged_at'].replace('Z', '+00:00')).timestamp()
        except (KeyError, TypeError, ValueError):
            require(False, 'merge_unverified')
        require(self.state['started_at'] <= merged_at <= self.factory.workflow.clock() + 60, 'merge_unverified')
        return {**result, 'status': 'smoke_verified', 'merged_at': merged_at}

    def handover(self, *, apply=False):
        if not self.state.get('smoke'):
            return {'status': 'smoke_pending'}
        observed = self.verify(self.factory.call('smoke_status'))
        if observed['status'] == 'handover_complete' or observed['status'] != 'smoke_verified' or not apply:
            return observed
        # The host independently reads GitHub again before its atomic activation.
        return self.factory.call('smoke_handover')

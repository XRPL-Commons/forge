"""A plain-language overview derived from current supervisor evidence."""

from .providers import available
from .contracts import NEXT_ROLE

def overview(data, repositories, runtime, observation, active, now):
    def result(title, detail, href=None, label=None, **extra):
        return {'title': title, 'detail': detail, 'action_url': href, 'action_label': label, **extra}
    if not active:
        return result('Finish setting up Forge', 'Connect a repository and verify setup before starting.', '/onboarding', 'Continue setup')
    if not runtime['enabled']:
        return result('Forge is paused', 'Issue processing is disabled. Existing worker cleanup continues.', '/settings', 'Review settings')
    age = now - observation.get('at', 0)
    if not observation.get('at') or not 0 <= age < max(120, runtime['cycle_seconds'] * 3):
        return result('Waiting for the Forge service', 'The supervisor has not reported recently. Saved work remains in place.', '/setup-help#host-installation', 'Check service setup')
    if observation.get('errors'):
        error = observation['errors'][0]
        return result('Forge needs attention', error['reason'], '/settings', 'Review setup')
    current = [i for i in data['issues'].values() if i['state'] not in ('merged', 'cancelled')]
    capacity = observation.get('capacity') or {}
    if capacity.get('allowed') is False and capacity.get('reason') in ('memory_budget', 'memory_headroom', 'memory_pressure', 'telemetry_unavailable'):
        unavailable = capacity['reason'] == 'telemetry_unavailable'
        return result('Waiting for memory checks' if unavailable else 'Waiting for available memory',
                      'Forge will retry automatically. Existing workers continue and queued issues keep their place.')
    working = next((i for i in current if i['active'] and not (i.get('ownership_wait') and data['jobs'][i['active']]['role'] == 'merge')), None)
    if working:
        job = data['jobs'][working['active']]
        step = {'build': 'Building', 'review': 'Reviewing', 'audit': 'Auditing', 'merge': 'Merging', 'recovery': 'Investigating'}.get(job['role'], 'Working on')
        return result(f"{step} issue #{working['number']}", working.get('title') or working['repo'], f"https://github.com/{working['repo']}/issues/{working['number']}", 'View issue')
    recovery = next((j for j in data['jobs'].values() if j['role'] == 'recovery' and not j.get('cleanup')), None)
    if recovery:
        issue = data['issues'][recovery['issue_key']]
        return result(f"Investigating issue #{issue['number']}", 'Forge is checking the failure before deciding the next attempt.',
                      f"https://github.com/{issue['repo']}/issues/{issue['number']}", 'View issue')
    waiting = next((i for i in current if i['state'] in ('approval_needed', 'hold', 'failed')), None)
    if waiting:
        if waiting.get('environment_hold'):
            return result('Forge’s build environment needs attention', 'The candidate pull request and code-retry budget are preserved.',
                          '/issues?issue=' + waiting['key'], 'Review environment blocker')
        if waiting.get('prerequisite_hold'):
            return result('Prepare the repository to continue', waiting['reason'],
                          '/settings/repositories/' + str(waiting['repo_id']) + '?setup=1', 'Check repository')
        return result('An issue needs your attention', waiting.get('reason') or 'Review the issue before continuing.', '/issues?issue=' + waiting.get('key', str(waiting.get('repo_id', '')) + ':' + str(waiting['number'])), 'Review issue')
    repos = [r for r in repositories if r['enabled']]
    if not repos:
        return result('Choose where Forge should work', 'No repositories are enabled.', '/settings/repositories', 'Choose a repository')
    blocked = next((r for r in repos if not r.get('access_ready') or not r.get('protection_ready')), None)
    if blocked:
        return result('Check repository setup', blocked['name'] + ' needs a current access and review-settings check.', blocked['setup_url'], 'Check repository')
    pool = runtime.get('provider_pool', [])
    unavailable = (not any((runtime.get('provider_ready') is None or entry['id'] in runtime['provider_ready'])
                           and available(entry, data, now) for entry in pool) if pool
                   else data.get('provider_retry_after', 0) > now)
    if current and unavailable:
        return result('Waiting for a coding account', 'A coding account is temporarily unavailable. Forge will retry when it can.', '/settings/harnesses', 'Review coding accounts')
    if current:
        queued = next((i for i in current if NEXT_ROLE.get(i['state']) and not i.get('ownership_wait') and not i.get('blocked_dependencies')), None)
        owned = next((i for i in current if i.get('ownership_wait')), None)
        if not queued and owned:
            return result(owned['ownership_wait']['headline'], owned['ownership_wait']['plain'],
                          '/issues?issue=' + owned['key'], 'View issue')
        if queued and runtime['slots'][NEXT_ROLE[queued['state']]] == 0:
            return result('Worker capacity is paused', 'The next stage has no worker slots enabled.', '/settings', 'Review worker capacity')
        if queued and queued.get('retry_after', 0) > now:
            return result('Waiting before retrying', queued.get('reason') or 'An earlier availability check failed. Forge will retry automatically.',
                          f"https://github.com/{queued['repo']}/issues/{queued['number']}", 'View issue')
        return result('Work is queued', 'The supervisor is checking eligibility and available worker capacity. Existing holds and ownership still apply.', '/settings', 'Review worker capacity')
    if not runtime['slots']['build']:
        return result('Builder capacity is paused', 'Enable a Builder slot to pick up ready issues.', '/settings', 'Review worker capacity')
    if any(not data.get('discovery_observed', {}).get(str(r['id'])) for r in repos):
        return result('Looking for ready issues', 'Forge is checking the enabled repositories. No work has been confirmed yet.')
    repo = repos[0]
    return result('Waiting for a ready issue', 'Choose an issue to queue for Forge. It will check eligibility before starting.', '/issues', 'Choose an issue', empty=True)

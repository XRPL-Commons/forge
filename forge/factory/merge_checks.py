"""Which pull requests carry forge/merge-policy, and the passing path for human PRs.

Scope ``all`` (the default) publishes forge/merge-policy on every open pull
request, so a repository can require the check without blocking people: a PR
Forge did not create passes it, and only the repository's own rules (reviews,
CI, signatures) decide its merge. Forge-created PRs pass only through the merge
controller's verified gates. Scope ``forge`` leaves human PRs untouched.

Publication is idempotent (one external ID per PR, one run per commit) and
read back from GitHub, so a lost response or a restart simply reconciles again.
"""
from .config import merge_check_scope
from .github import GitHubUnavailable

MERGE_POLICY = 'forge/merge-policy'


def merge_policy_rule(app_id):
    """The baseline rule that requires forge/merge-policy from the Forge App only."""
    if type(app_id) is not int or app_id <= 0:
        raise ValueError('The Forge App ID is required to pin forge/merge-policy')
    return {'type': 'required_status_checks', 'parameters': {
        'strict_required_status_checks_policy': False,
        'required_status_checks': [{'context': MERGE_POLICY, 'integration_id': app_id}]}}


class HumanMergeChecks:
    def __init__(self, github, runtime, *, limit=50):
        self.github, self.runtime, self.limit = github, runtime, limit

    def reconcile(self, item, pulls=None):
        """Publish the human passing check for one repository's open pull requests."""
        if merge_check_scope(self.runtime, item['repo_id']) != 'all':
            return []
        pulls = self.github.pulls(item) if pulls is None else pulls
        forge_heads = {(pr.get('head') or {}).get('sha') for pr in pulls if pr.get('forge_owned')}
        results = []
        for pr in pulls:
            if len(results) >= self.limit:
                break
            head = (pr.get('head') or {}).get('sha')
            if (pr.get('forge_owned') or pr.get('state') != 'open' or pr.get('draft') is True
                    or ((pr.get('base') or {}).get('repo') or {}).get('id') != item['repo_id']):
                continue
            if head in forge_heads:
                # A commit shared with a Forge candidate is governed by its gates.
                results.append({'pull_number': pr.get('number'), 'head': head, 'status': 'forge_candidate'})
                continue
            try:
                check_id = self.github.human_merge_policy(item, pr)
            except ValueError:
                continue
            except GitHubUnavailable as error:
                results.append({'pull_number': pr['number'], 'head': head, 'status': 'unconfirmed',
                                'http_status': error.status})
                continue
            results.append({'pull_number': pr['number'], 'head': head,
                            'status': 'forge_candidate' if check_id is None else 'published', 'check_id': check_id})
        return results

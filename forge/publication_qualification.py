"""Host-owned signing probes. Only explicitly selected scratch refs are written."""
import argparse
import base64
import json
import re
import time
from urllib.parse import quote
import uuid

from .app_access import AccessError, REPO_PATTERN
from .factory.contracts import digest
from .factory.github import GitHub, GitHubUnavailable
from .factory.publisher import COMMIT
from .settings import Settings
from .store.configuration import Configuration
from .store.runtime import selected
from .request_metrics import request_scope, RequestBudgetExhausted


class QualificationError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise QualificationError(reason)


def oid(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'invalid_git_identity')
    return value


def branch(value):
    require(isinstance(value, str) and 0 < len(value) <= 200 and
            not any(c in value for c in '~^:?*[\\') and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
            and not any(s in value for s in ('..', '@{', '//')) and not value.startswith(('/', '-'))
            and not value.endswith(('/', '.', '.lock')), 'invalid_branch')
    return value


def signing_context(call, ref):
    """Observe only signing requirements; unknown classic access is not absence."""
    ref = branch(ref)
    existing = call('GET', '/branches/' + quote(ref, safe=''))
    require(existing.get('name') == ref, 'branch_identity_changed')
    try:
        classic = call('GET', '/branches/' + quote(ref, safe='') + '/protection')
    except (AccessError, GitHubUnavailable) as error:
        if error.status != 404:
            raise
        classic = {}
    require(isinstance(classic, dict), 'classic_signing_rule_unavailable')
    if classic:
        try:
            signature = call('GET', '/branches/' + quote(ref, safe='') + '/protection/required_signatures')
        except (AccessError, GitHubUnavailable) as error:
            if error.status != 404:
                raise
            signature = {'enabled': False}
        require(signature.get('enabled') is False, 'classic_signing_rule_unsupported')
    rows = call('GET', '/rules/branches/' + quote(ref, safe='') + '?per_page=100')
    require(isinstance(rows, list) and len(rows) < 100, 'signing_rules_unavailable')
    sources = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('type'), str), 'signing_rules_unavailable')
        if row['type'] != 'required_signatures':
            continue
        identifier = row.get('ruleset_id')
        require(type(identifier) is int and identifier > 0, 'signing_rules_unavailable')
        rule = call('GET', '/rulesets/' + str(identifier))
        require(rule.get('id') == identifier and rule.get('enforcement') == 'active'
                and rule.get('current_user_can_bypass') == 'never', 'signing_rule_bypass_unproven')
        require(rule.get('source_type') == 'Organization', 'repository_signing_rule_unsupported')
        # Same immutable ruleset identity and relevant current configuration must
        # apply to target and scratch; another organization's rule is not proof.
        fields = {key: rule.get(key) for key in ('id', 'source_type', 'source', 'enforcement', 'conditions', 'rules', 'bypass_actors')}
        require(isinstance(fields['rules'], list) and any(r.get('type') == 'required_signatures' for r in fields['rules']),
                'signing_rules_unavailable')
        sources[identifier] = {'id': identifier, 'source': fields['source'], 'digest': digest(fields)}
    return sorted(sources.values(), key=lambda row: row['id'])


class Access:
    """Target read token and scratch write token are separate, repository scoped."""
    def __init__(self, settings, *, installation_targets=()):
        self.settings = settings
        self.installation_targets = installation_targets
        self.github = GitHub(settings, {})
        self.client = settings.client
        self.tokens = []

    def mint(self, connection, ids, *, write=False, workflows=False):
        require(not workflows or self.permissions.get('workflows') == 'write', 'workflows_grant_missing')
        permissions = {'contents': 'write' if write else 'read', 'metadata': 'read', 'administration': 'read'}
        if workflows:
            permissions['workflows'] = 'write'
        value = self.client.request('POST', '/app/installations/' + str(connection['installation_id']) + '/access_tokens',
                                    self.client.jwt(connection), {'repository_ids': ids, 'permissions': permissions})
        token = value.get('token')
        require(isinstance(token, str) and token, 'installation_access_missing')
        self.tokens.append(token)
        require(value.get('permissions', {}).get('contents') == permissions['contents']
                and all(level in ('read', 'none') or key == 'contents' and write and level == 'write'
                        or key == 'workflows' and workflows and level == 'write'
                        for key, level in value.get('permissions', {}).items()), 'token_scope_changed')
        return token

    def open(self, target_name, scratch_name, *, write=False):
        configured = self.settings.load()
        matches = [row for row in configured['repositories'] if row.get('name') == target_name]
        if not matches:
            matches = [row for row in self.installation_targets if row.get('name') == target_name]
        # An operator must connect the target first.
        require(len(matches) == 1, 'target_repository_not_configured')
        repository_id = matches[0].get('id', matches[0].get('repo_id'))
        connection = self.github.connection('controller', repository_id)
        installation = self.client.request('GET', '/app/installations/' + str(connection['installation_id']), self.client.jwt(connection))
        require(installation.get('id') == connection['installation_id'] and installation.get('app_id') == connection['app_id']
                and installation.get('suspended_at') is None, 'installation_changed')
        self.connection, self.permissions = connection, installation.get('permissions') or {}
        require(self.permissions.get('contents') == 'write' and self.permissions.get('administration') in ('read', 'write'),
                'installation_access_missing')
        target_token = self.mint(connection, [repository_id])
        self.target = lambda method, suffix, payload=None: self.client.request(method, '/repos/' + target_name + suffix, target_token, payload)
        target = self.target('GET', '')
        # Discover scratch ID with a metadata-only installation token, then use
        # a precise scratch-only token. Personal credentials are never accepted.
        discovery = self.client.request('POST', '/app/installations/' + str(connection['installation_id']) + '/access_tokens',
                                        self.client.jwt(connection), {'permissions': {'metadata': 'read'}})
        discovery_token = discovery.get('token')
        require(isinstance(discovery_token, str) and discovery_token, 'installation_access_missing')
        self.tokens.append(discovery_token)
        require(discovery.get('permissions') == {'metadata': 'read'}, 'token_scope_changed')
        scratch = self.client.request('GET', '/repos/' + scratch_name, discovery_token)
        require(target.get('id') == repository_id and target.get('full_name') == target_name
                and scratch.get('full_name') == scratch_name and type(scratch.get('id')) is int
                and scratch['id'] > 0 and scratch['id'] != repository_id and scratch.get('private') is True
                and type((target.get('owner') or {}).get('id')) is int
                and (target.get('owner') or {}).get('id') == (scratch.get('owner') or {}).get('id')
                == (installation.get('account') or {}).get('id'), 'repository_identity_changed')
        self.scratch_id, self.scratch_name = scratch['id'], scratch_name
        self.scratch_token = self.mint(connection, [scratch['id']], write=write)
        self.scratch = lambda method, suffix, payload=None: self.client.request(method, '/repos/' + scratch_name + suffix, self.scratch_token, payload)
        app = self.client.request('GET', '/app', self.client.jwt(connection))
        require(app.get('id') == connection['app_id'] and isinstance(app.get('slug'), str), 'app_identity_changed')
        login = app['slug'] + '[bot]'
        user = self.client.request('GET', '/users/' + quote(login, safe=''), target_token)
        require(user.get('login') == login and user.get('type') == 'Bot' and type(user.get('id')) is int, 'app_identity_changed')
        self.bot = {key: user[key] for key in ('id', 'login', 'type')}
        return target, scratch, connection

    def graphql(self, variables):
        result = self.client.request('POST', '/graphql', self.scratch_token, {'query': COMMIT, 'variables': variables})
        require(isinstance(result, dict) and not result.get('errors'), 'graphql_refused')
        return result['data']['createCommitOnBranch']['commit']['oid']

    def close(self):
        failed = False
        for token in self.tokens:
            try:
                self.client.request('DELETE', '/installation/token', token)
            except Exception:
                failed = True
        self.tokens.clear()
        require(not failed, 'credential_revocation_unconfirmed')


class Qualification:
    def __init__(self, store, access, *, clock=time.time):
        self.configuration, self.access, self.clock = Configuration(store), access, clock

    def save(self, receipt):
        domain = 'publication_workflow_probe' if receipt.get('purpose') == 'workflow' else 'publication_signing_probe'
        current = self.configuration.get(domain, receipt['id'])
        self.configuration.replace(domain, receipt,
            expected_revision=current['revision'] if current else 0, subject=receipt['id'])

    def head(self, ref):
        value = self.access.scratch('GET', '/git/ref/heads/' + quote(ref, safe=''))
        require(value.get('ref') == 'refs/heads/' + ref and value.get('object', {}).get('type') == 'commit', 'probe_ref_changed')
        return oid(value['object']['sha'])

    def commit(self, value):
        raw = self.access.scratch('GET', '/git/commits/' + oid(value))
        require(raw.get('sha') == value, 'probe_commit_changed')
        return {'oid': value, 'tree_oid': oid(raw['tree']['sha']), 'parents': [oid(p['sha']) for p in raw['parents']],
                'verified': raw.get('verification', {}).get('verified') is True, 'message': raw.get('message')}

    def operation(self, receipt, strategy):
        ref, marker = receipt['probe_ref'], 'Forge signing qualification ' + receipt['id']
        parent = self.head(ref)
        previous = self.commit(parent)
        content = (marker + '\n').encode()
        path = 'forge-signing-qualification-' + receipt['id'] + '.txt'
        if receipt.get('purpose') == 'workflow':
            path = '.github/workflows/forge-signing-qualification-' + receipt['id'] + '.yml'
            content = b'name: Forge permission probe\non: workflow_dispatch\npermissions: {}\njobs:\n  probe:\n    if: false\n    runs-on: ubuntu-latest\n    steps:\n      - run: \'true\'\n'
        receipt.update(stage='tree_started', strategy=strategy, parent_oid=parent)
        self.save(receipt)
        tree = self.access.scratch('POST', '/git/trees', {'base_tree': previous['tree_oid'],
            'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'content': content.decode()}]})
        tree_oid = oid(tree['sha'])
        receipt.update(stage='commit_started', tree_oid=tree_oid)
        self.save(receipt)
        if strategy == 'rest':
            created = self.access.scratch('POST', '/git/commits', {'message': marker, 'tree': tree_oid, 'parents': [parent]})
            commit_oid = oid(created['sha'])
        else:
            commit_oid = oid(self.access.graphql({'input': {'branch': {'repositoryNameWithOwner': receipt['probe_repository'], 'refName': ref},
                'expectedHeadOid': parent, 'message': {'headline': marker},
                'fileChanges': {'additions': [{'path': path, 'contents': base64.b64encode(content).decode()}]},
                'clientMutationId': receipt['id']}}))
        observed = self.commit(commit_oid)
        require(observed['tree_oid'] == tree_oid and observed['parents'] == [parent] and observed['message'] == marker,
                'probe_commit_changed')
        receipt.update(stage='commit_observed', commit_oid=commit_oid, verified=observed['verified'])
        self.save(receipt)
        if not observed['verified']:
            require(strategy == 'rest' and self.head(ref) == parent, 'unsigned_probe_or_ref_changed')
            return False
        linked = self.access.scratch('GET', '/commits/' + commit_oid)
        require(linked.get('sha') == commit_oid and all((linked.get('author') or {}).get(k) == v for k, v in self.access.bot.items()),
                'probe_author_changed')
        if strategy == 'rest':
            receipt['stage'] = 'ref_started'; self.save(receipt)
            # Exact expected-head CAS, matching the production publisher.
            result = self.access.client.request('POST', '/graphql', self.access.scratch_token, {
                'query': 'mutation($input:UpdateRefsInput!){updateRefs(input:$input){clientMutationId}}',
                'variables': {'input': {'repositoryId': receipt['probe_node_id'], 'refUpdates': [
                    {'name': 'refs/heads/' + ref, 'beforeOid': parent, 'afterOid': commit_oid, 'force': False}]}}})
            require(isinstance(result, dict) and not result.get('errors') and 'data' in result, 'ref_outcome_unconfirmed')
        require(self.head(ref) == commit_oid, 'probe_ref_changed')
        receipt.update(stage='ref_observed', exact_ref=True)
        self.save(receipt)
        return True

    def reconcile(self, receipt):
        """A lost write response may be proven by reads; it is never replayed."""
        stage = receipt.get('stage')
        if stage != 'ref_started' and not (stage in ('commit_started', 'commit_observed') and receipt.get('strategy') == 'graphql'):
            return
        head = self.head(receipt['probe_ref'])
        if stage == 'ref_started':
            require(head == receipt['commit_oid'], 'probe_write_unresolved')
        else:
            require(head != receipt['parent_oid'], 'probe_write_unresolved')
        raw = self.commit(head)
        linked = self.access.scratch('GET', '/commits/' + head)
        require(raw['parents'] == [receipt['parent_oid']] and raw['tree_oid'] == receipt['tree_oid']
                and raw['message'] == 'Forge signing qualification ' + receipt['id'] and raw['verified']
                and linked.get('sha') == head and all((linked.get('author') or {}).get(k) == v for k, v in self.access.bot.items()),
                'probe_write_unresolved')
        receipt.update(stage='ref_observed', commit_oid=head, verified=True, exact_ref=True)
        self.save(receipt)

    def verify(self, receipt, target_context, scratch_context):
        require(target_context == receipt['target_rules'] and scratch_context == receipt['probe_rules'], 'signing_rules_changed')
        require(receipt.get('stage') in ('ref_observed', 'qualified'), 'probe_evidence_incomplete')
        observed = self.commit(receipt['commit_oid'])
        require(receipt.get('verified') is True
                and observed['verified'] and observed['tree_oid'] == receipt['tree_oid']
                and observed['parents'] == [receipt['parent_oid']] and self.head(receipt['probe_ref']) == receipt['commit_oid'],
                'probe_evidence_incomplete')
        if target_context:
            # Rule-suite evidence distinguishes an enforced pass from bypass.
            rows = self.access.scratch('GET', '/rulesets/rule-suites?ref=' + quote('refs/heads/' + receipt['probe_ref'], safe='') + '&evaluate_status=active&per_page=100')
            require(isinstance(rows, list) and len(rows) <= 100, 'rule_suite_unavailable')
            matching = [row for row in rows if row.get('after_sha') == receipt['commit_oid'] and row.get('before_sha') == receipt['parent_oid']
                        and row.get('ref') == 'refs/heads/' + receipt['probe_ref'] and row.get('actor_id') == self.access.bot['id']]
            require(bool(matching), 'rule_suite_pending')
            suite = self.access.scratch('GET', '/rulesets/rule-suites/' + str(matching[0]['id']))
            require(suite.get('result') == 'pass' and suite.get('repository_id') == receipt['probe_repository_id']
                    and suite.get('before_sha') == receipt['parent_oid'] and suite.get('after_sha') == receipt['commit_oid']
                    and suite.get('ref') == 'refs/heads/' + receipt['probe_ref'] and suite.get('actor_id') == self.access.bot['id'], 'rule_suite_not_accepted')
            passed = {row.get('rule_source', {}).get('id') for row in suite.get('rule_evaluations', [])
                      if row.get('rule_type') == 'required_signatures' and row.get('enforcement') == 'active' and row.get('result') == 'pass'}
            require({row['id'] for row in target_context} <= passed, 'signing_rule_not_accepted')
            receipt['rule_suite_id'] = suite['id']

    def workflow(self, receipt):
        """Separate bounded cases; neither changes the ordinary signing proof."""
        checks = receipt.setdefault('workflow_checks', {})
        original_token = self.access.scratch_token
        cases = [('without_workflows', False)]
        if self.access.permissions.get('workflows') == 'write':
            cases.append(('with_existing_grant', True))
        for name, granted in cases:
            identifier = checks.get(name, {}).get('receipt_id')
            if identifier:
                if checks[name]['status'] in ('started', 'unconfirmed'):
                    recorded = self.configuration.get('publication_workflow_probe', identifier)
                    if recorded:
                        case = recorded['settings']
                        try:
                            self.reconcile(case)
                            require(case.get('stage') == 'ref_observed' and self.head(case['probe_ref']) == case['commit_oid'],
                                    'probe_write_unresolved')
                            checks[name]['status'] = 'accepted'
                        except (AccessError, GitHubUnavailable, QualificationError, RequestBudgetExhausted):
                            checks[name]['status'] = 'unconfirmed'
                        self.save(receipt)
                if checks[name]['status'] == 'unconfirmed':
                    break
                continue
            case = {key: receipt[key] for key in ('probe_ref', 'probe_repository', 'probe_repository_id', 'probe_node_id')}
            case.update(id=uuid.uuid4().hex, purpose='workflow', stage='prepared', strategy=receipt['strategy'])
            self.save(case)
            checks[name] = {'receipt_id': case['id'], 'status': 'started'}
            self.save(receipt)
            if granted:
                self.access.scratch_token = self.access.mint(self.access.connection, [receipt['probe_repository_id']], write=True, workflows=True)
            try:
                accepted = self.operation(case, receipt['strategy'])
                checks[name]['status'] = 'accepted' if accepted else 'unsigned'
            except (AccessError, GitHubUnavailable, QualificationError, RequestBudgetExhausted) as error:
                # An HTTP denial proves refusal, not its permission cause.
                status = getattr(error, 'status', None)
                try:
                    unchanged = case.get('parent_oid') is not None and self.head(case['probe_ref']) == case['parent_oid']
                except (AccessError, GitHubUnavailable, RequestBudgetExhausted):
                    unchanged = None
                refused = status in (401, 403, 404, 422) or isinstance(error, QualificationError) and str(error) == 'graphql_refused'
                checks[name].update(status='refused' if refused and unchanged else 'unconfirmed',
                                    http_status=status, unchanged_ref=unchanged)
            finally:
                self.access.scratch_token = original_token
                self.save(receipt)
            if checks[name]['status'] == 'unconfirmed':
                break
        return dict(status='observed', receipt_id=receipt['id'], workflow_checks=checks,
                    positive_test=('observed' if 'with_existing_grant' in checks else 'waiting_for_previous_case') if len(cases) == 2 else 'existing_workflows_grant_missing',
                    permission_cause_proven=False, ordinary_qualification_unchanged=True)

    def execute(self, target_name, scratch_name, *, target_branch=None, probe_ref=None, apply=False, receipt_id=None, check_workflow=False, request_id=None):
        require(REPO_PATTERN.fullmatch(target_name or '') and REPO_PATTERN.fullmatch(scratch_name or '') and target_name != scratch_name,
                'invalid_repository')
        require(receipt_id is None or re.fullmatch('[a-f0-9]{32}', receipt_id), 'invalid_receipt')
        require(request_id is None or isinstance(request_id, str) and re.fullmatch('[a-f0-9]{32}', request_id)
                and receipt_id is None, 'invalid_request_id')
        identifier = receipt_id or request_id
        previous = self.configuration.get('publication_signing_probe', identifier) if identifier else None
        require(not receipt_id or previous is not None, 'probe_receipt_missing')
        saved = previous['settings'] if previous else None
        if saved:
            require(saved['repository'] == target_name and saved['probe_repository'] == scratch_name, 'probe_receipt_identity_changed')
            target_branch = target_branch or saved['target_branch']; probe_ref = probe_ref or saved['probe_ref']
        require(not check_workflow or saved and saved.get('stage') == 'qualified', 'workflow_check_requires_qualified_receipt')
        target, scratch, connection = self.access.open(target_name, scratch_name, write=apply and (not saved or check_workflow))
        target_branch, probe_ref = branch(target_branch or target['default_branch']), branch(probe_ref or scratch['default_branch'])
        target_rules = signing_context(self.access.target, target_branch)
        probe_rules = signing_context(self.access.scratch, probe_ref)
        require(not target_rules or all(row in probe_rules for row in target_rules), 'probe_signing_rules_do_not_match_target')
        context = dict(repository=target_name, repository_id=target['id'], target_branch=target_branch,
            probe_repository=scratch_name, probe_repository_id=scratch['id'], probe_node_id=scratch['node_id'], probe_ref=probe_ref,
            app_id=connection['app_id'], installation_id=connection['installation_id'], target_rules=target_rules, probe_rules=probe_rules)
        if saved:
            require(all(saved.get(k) == v for k, v in context.items()), 'probe_receipt_identity_changed')
        if not apply:
            return {'status': 'planned', **context, 'receipt_id': receipt_id,
                    'effects': (['scratch workflow-file commit and ref update with current grants', 'host workflow receipt'] if check_workflow else
                        ['scratch commit and ref update', 'host qualification receipt'] if not saved else ['verify existing host receipt', 'host qualification receipt']),
                    'workflows_granted': self.access.permissions.get('workflows') == 'write', 'workflow_test': 'separate_probe_required'}
        if check_workflow:
            return self.workflow(saved)
        receipt = saved or dict(id=request_id or uuid.uuid4().hex, version=2, stage='prepared', observed_at=self.clock(), **context)
        try:
            if not saved:
                self.save(receipt)
                if not self.operation(receipt, 'rest'):
                    receipt['rest_unsigned'] = True
                    self.operation(receipt, 'graphql')
            self.reconcile(receipt)
            self.verify(receipt, signing_context(self.access.target, target_branch), signing_context(self.access.scratch, probe_ref))
            proof = {key: receipt[key] for key in ('repository_id', 'app_id', 'installation_id', 'probe_repository_id', 'commit_oid', 'tree_oid',
                                                  'target_branch', 'target_rules', 'probe_ref', 'strategy')}
            proof.update(version=2, probe_receipt_id=receipt['id'], verified=True, exact_ref=True,
                signing_requirement='accepted' if target_rules else 'not_required', signing_rule_accepted=bool(target_rules), observed_at=receipt['observed_at'])
            current = self.configuration.get('publication_signing', str(target['id']))
            if not current or current['settings'] != proof:
                revision = self.configuration.replace('publication_signing', proof, expected_revision=current['revision'] if current else 0, subject=str(target['id']))
            else:
                revision = current['revision']
            receipt['stage'] = 'qualified'; self.save(receipt)
            return dict(status='qualified', receipt_id=receipt['id'], strategy=receipt['strategy'], qualification_revision=revision,
                        signing_requirement=proof['signing_requirement'], workflow_test='separate_probe_required',
                        workflows_granted=self.access.permissions.get('workflows') == 'write')
        except (QualificationError, AccessError, GitHubUnavailable, RequestBudgetExhausted) as error:
            reason = str(error) if isinstance(error, QualificationError) else 'github_observation_unconfirmed'
            receipt['reason'] = reason; self.save(receipt)
            return dict(status='pending', receipt_id=receipt['id'], reason=reason, stage=receipt['stage'],
                        next_action='Use --receipt ' + receipt['id'] + ' --apply to re-read this attempt; writes are never replayed.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    import sys
    value = json.loads(sys.stdin.buffer.read(8193))
    require(isinstance(value, dict) and set(value) == {'repo', 'probe_repo', 'target_branch', 'probe_ref', 'apply', 'receipt', 'check_workflow'}
            and type(value['apply']) is bool and type(value['check_workflow']) is bool, 'invalid_request')
    store = selected(args.root)
    require(store is not None, 'sqlite_runtime_required')
    access = Access(Settings(args.root, scope='active'))
    try:
        with request_scope(until=time.monotonic() + 210):
            report = Qualification(store, access).execute(value['repo'], value['probe_repo'], target_branch=value['target_branch'],
                probe_ref=value['probe_ref'], apply=value['apply'], receipt_id=value['receipt'], check_workflow=value['check_workflow'])
    except (QualificationError, AccessError, GitHubUnavailable, RequestBudgetExhausted) as error:
        report = {'status': 'blocked', 'reason': str(error) if isinstance(error, QualificationError) else 'github_observation_unconfirmed'}
    finally:
        try:
            with request_scope(until=time.monotonic() + 70):
                access.close()
        except QualificationError:
            report = {'status': 'blocked', 'reason': 'credential_revocation_unconfirmed'}
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

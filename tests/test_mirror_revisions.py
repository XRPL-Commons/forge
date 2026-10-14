import unittest
from unittest.mock import patch

from forge.mirror.policy import MirrorError, Repository
from forge.mirror.revisions import _range, resolve_revision
from tests.test_mirror_policy import Reader
from forge.mirror.dependencies import dependency_closure, extract


class RevisionTests(unittest.TestCase):
    def test_stable_semver_selectors_match_expected_bounds(self):
        for selector, accepted, refused in [
            ('^1.2.3', (1, 9, 9), (2, 0, 0)), ('^0.2.3', (0, 2, 9), (0, 3, 0)),
            ('^0.0.3', (0, 0, 3), (0, 0, 4)), ('~1.2', (1, 2, 99), (1, 3, 0)),
            ('1.x', (1, 50, 0), (2, 0, 0)), ('>=1.2.3 <2.0.0', (1, 5, 0), (2, 0, 0))]:
            with self.subTest(selector=selector):
                predicate = _range(selector)
                self.assertTrue(predicate(accepted))
                self.assertFalse(predicate(refused))
        for selector in ('1.x.2', '^1.2.3-beta', 'arbitrary || commands'):
            with self.assertRaises(MirrorError):
                _range(selector)

    def test_branch_tag_ambiguity_refused_and_semver_chooses_highest_stable_tag(self):
        class Cache:
            def path(self, _): return '/trusted/cache'
        repository = Repository(1, 'org/repo', 10, 'Organization', 20)
        def git(args, **kwargs):
            if 'check-ref-format' in args: return b''
            if 'for-each-ref' in args: return b'v1.2.0\nv1.9.1\nv2.0.0\nv1.10.0-beta\n'
            if args[-1] == 'refs/tags/v1.9.1^{commit}': return b'a' * 40 + b'\n'
            if args[-1].startswith('refs/heads/'): return b'b' * 40 + b'\n'
            return b'c' * 40 + b'\n'
        with patch('forge.mirror.revisions.run_git', side_effect=git):
            self.assertEqual(resolve_revision(Cache(), repository, 'semver:^1.2.0'), 'a' * 40)
            with self.assertRaisesRegex(MirrorError, 'dependency_format_unsupported'):
                resolve_revision(Cache(), repository, 'ambiguous')

    def test_pip_and_npm_refs_remain_in_immutable_declaration_evidence(self):
        reader = Reader({'package.json': '{"dependencies":{"a":"github:vendor/npm#semver:^1.2.0"}}',
                         'requirements.txt': 'dep @ git+https://github.com/vendor/pip.git@v3#subdirectory=src'})
        result = extract(reader, 1, 'org/source', ['a' * 40])
        self.assertEqual({row['canonical_name']: row['source_evidence']['dependency_ref'] for row in result.repositories},
                         {'vendor/npm': 'semver:^1.2.0', 'vendor/pip': 'v3'})

    def test_transitive_closure_records_exact_chain_and_refusal_never_grants_children(self):
        root = Reader({'package.json': '{"dependencies":{"a":"github:vendor/one#v1","bad":"github:vendor/denied"}}'})
        one = Reader({'package.json': '{"dependencies":{"two":"github:vendor/two"}}'})
        two = Reader({'package.json': '{}'})
        calls = []
        def resolve(item):
            calls.append(item['canonical_name'])
            if item['canonical_name'] == 'vendor/denied': return None
            return ((2, 'vendor/one', one, 'b' * 40) if item['canonical_name'] == 'vendor/one'
                    else (3, 'vendor/two', two, 'c' * 40))
        result = dependency_closure(root, 1, 'org/source', ['a' * 40], resolve_pinned=resolve)
        nested = next(item for item in result.repositories if item['canonical_name'] == 'vendor/two')
        self.assertEqual(nested['source_evidence']['commit_oid'], 'b' * 40)
        self.assertEqual(nested['source_evidence']['parent_declarations'][0]['dependency_ref'], 'v1')
        self.assertEqual(nested['source_evidence']['resolved_commit_oid'], 'c' * 40)
        self.assertEqual(set(calls), {'vendor/one', 'vendor/two', 'vendor/denied'})

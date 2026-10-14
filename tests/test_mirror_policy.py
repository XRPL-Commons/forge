import hashlib
import json
import unittest

from forge.mirror.dependencies import extract, relative_submodule, submodule_closure
from forge.mirror.policy import MirrorError, Repository, authorize, github_repository, policy_digest


def policy():
    return dict(version=1, project_repository_id=1, canonical_repository='acme/project', revision='r1',
                manifest_digest='a' * 64, declared_repositories=[{'canonical_name': 'vendor/private', 'repository_id': 2}],
                organization_installations=[{'account_id': 10, 'installation_id': 20, 'app_id': 30}],
                denied_repositories=[], extractor_version=1)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.live = self.policy['organization_installations']

    def check(self, repo, **kw):
        return authorize(self.policy, repo, live_installations=kw.pop('live', self.live), **kw)

    def test_declared_and_org_access_are_separate_from_installation_access(self):
        declared = Repository(2, 'vendor/private', 12, 'User', 22, actual_access=True)
        org = Repository(3, 'acme/another', 10, 'Organization', 20, actual_access=True)
        self.assertEqual(self.check(declared), 'declared')
        self.assertEqual(self.check(org), 'organization')
        public = Repository(5, 'acme/public', 10, 'Organization', None, actual_access=True)
        self.assertEqual(self.check(public), 'organization')
        with self.assertRaisesRegex(MirrorError, 'installation_access_missing'):
            self.check(Repository(3, 'acme/another', 10, 'Organization', 20))
        with self.assertRaisesRegex(MirrorError, 'policy_denied'):
            self.check(Repository(4, 'personal/private', 10, 'User', 20, actual_access=True))

    def test_live_narrowing_and_id_deny_precedence_cover_renames_and_cache(self):
        renamed = Repository(2, 'changed/name', 12, 'User', 22, actual_access=True)
        self.assertEqual(self.check(renamed), 'declared')
        with self.assertRaisesRegex(MirrorError, 'project_denied'):
            self.check(renamed, live_denied=[{'canonical_name': 'vendor/private', 'repository_id': 2}])
        org = Repository(3, 'acme/another', 10, 'Organization', 20, actual_access=True)
        with self.assertRaisesRegex(MirrorError, 'policy_denied'):
            self.check(org, live=[])
        with self.assertRaisesRegex(MirrorError, 'policy_denied'):
            self.check(Repository(99, 'vendor/private', 12, 'User', 22, actual_access=True))

    def test_new_installation_does_not_expand_frozen_policy(self):
        addition = {'account_id': 11, 'installation_id': 21, 'app_id': 31}
        with self.assertRaisesRegex(MirrorError, 'policy_denied'):
            self.check(Repository(4, 'other/repo', 11, 'Organization', 21, actual_access=True), live=self.live + [addition])
        self.assertEqual(len(policy_digest(self.policy)), 64)

    def test_supported_git_urls_and_invalid_transport_or_credentials(self):
        for value in ('git@github.com:ACME/Repo.git', 'git+ssh://git@github.com/acme/repo.git#main',
                      'https://github.com/acme/repo', 'github:acme/repo#v1', 'git://github.com/acme/repo.git'):
            self.assertEqual(github_repository(value), 'acme/repo')
        for value in ('https://token@github.com/acme/repo', 'https://github.com/acme/repo%2fother',
                      'https://github.com/acme/../repo', 'ssh://bad@github.com/acme/repo',
                      'https://github.com.evil/acme/repo', 'file:///private/repo', 'ext::sh -c bad'):
            with self.subTest(value=value), self.assertRaises((MirrorError, ValueError)):
                github_repository(value)


class Reader:
    def __init__(self, documents, gitlinks=None):
        self.blobs = {}
        self.entries = []
        for path, text in documents.items():
            raw = text.encode()
            oid = hashlib.sha1(raw).hexdigest()
            self.blobs[oid] = raw
            self.entries.append(dict(path=path, mode='100644', oid=oid))
        for path, oid in (gitlinks or {}).items():
            self.entries.append(dict(path=path, mode='160000', oid=oid))

    def list_tree(self, commit):
        return self.entries

    def read_blob(self, oid):
        return self.blobs[oid]


class DeclarationTests(unittest.TestCase):
    def test_pinned_package_lock_workspaces_python_and_gitlinks_have_blob_provenance(self):
        reader = Reader({
            'package.json': json.dumps({'dependencies': {'a': 'github:vendor/one'}, 'workspaces': ['packages/*'],
                                        'scripts': {'bad': 'github:forbidden/script'}}),
            'packages/sub/package.json': json.dumps({'devDependencies': {'b': 'vendor/two#main'}}),
            'other/package.json': json.dumps({'dependencies': {'bad': 'github:forbidden/unreferenced'}}),
            'package-lock.json': json.dumps({'lockfileVersion': 3, 'packages': {'node_modules/x': {'resolved': 'git+https://github.com/vendor/three.git#123'}}}),
            'requirements.txt': 'thing @ git+https://github.com/vendor/four.git@main#subdirectory=src\n-r other.txt\n',
            'other.txt': '-e git+https://github.com/vendor/five.git@main#egg=thing\n',
            'pyproject.toml': '[project]\ndependencies=["dep @ git+https://github.com/vendor/six.git"]\n[tool.uv.sources]\ndep={git="https://github.com/vendor/seven"}\n',
            '.gitmodules': '[submodule "sub"]\npath = sub\nurl = ../eight.git\n',
        }, {'sub': 'b' * 40})
        result = extract(reader, 1, 'vendor/project', ['a' * 40])
        self.assertEqual({r['canonical_name'] for r in result.repositories}, {'vendor/' + name for name in
            ('one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight')})
        for row in result.repositories:
            self.assertEqual(row['source_evidence']['commit_oid'], 'a' * 40)
            self.assertEqual(len(row['source_evidence']['blob_oid']), 40)
        self.assertEqual(result.submodules[0]['commit_oid'], 'b' * 40)
        self.assertEqual(result.unsupported, [])

    def test_lock_resolution_subset_and_no_arbitrary_prose_authority(self):
        reader = Reader({'pnpm-lock.yaml': 'packages:\n  a:\n    resolution: {repo: git+https://github.com/org/private.git, commit: abc}\n',
                         'yarn.lock': 'a:\n  resolved "git+ssh://git@github.com/org/yarn.git#abc"\n',
                         'README.md': 'https://github.com/forbidden/readme',
                         'package.json': '{"description":"github:forbidden/text"}'})
        result = extract(reader, 1, 'org/source', ['a' * 40])
        self.assertEqual({r['canonical_name'] for r in result.repositories}, {'org/private', 'org/yarn'})

    def test_unsupported_git_host_is_owned_evidence_and_bounds_do_not_run_scripts(self):
        result = extract(Reader({'package.json': '{"dependencies":{"dep":"git+https://other.example/repo.git"}}'}),
                         1, 'org/source', ['a' * 40])
        self.assertFalse(result.repositories)
        self.assertEqual(result.unsupported[0]['reason'], 'dependency_format_unsupported')
        with self.assertRaisesRegex(MirrorError, 'mirror_capacity'):
            extract(Reader({'requirements.txt': 'x' * (4 * 1024 * 1024 + 1)}), 1, 'org/source', ['a' * 40])
        with self.assertRaises(MirrorError):
            relative_submodule('../../../private', 'org/source')


class ClosureTests(unittest.TestCase):
    def test_nested_submodule_declarations_are_bound_to_parent_gitlink(self):
        outer = Reader({'.gitmodules': '[submodule "inner"]\npath=inner\nurl=../inner.git\n'}, {'inner': 'b' * 40})
        inner = Reader({'package.json': '{"dependencies":{"nested":"github:vendor/nested"}}'})
        calls = []
        def resolve(item):
            calls.append(item)
            return 2, 'acme/inner', inner
        result = submodule_closure(outer, 1, 'acme/outer', ['a' * 40], resolve_pinned=resolve)
        self.assertEqual(calls[0]['commit_oid'], 'b' * 40)
        nested = next(item for item in result.repositories if item['canonical_name'] == 'vendor/nested')
        self.assertEqual(nested['source_evidence']['commit_oid'], 'b' * 40)
        self.assertEqual(nested['source_evidence']['parent_declarations'][0]['commit_oid'], 'a' * 40)
        with self.assertRaisesRegex(MirrorError, 'mirror_capacity'):
            submodule_closure(outer, 1, 'acme/outer', ['a' * 40], resolve_pinned=resolve, max_depth=0)

    def test_python_lock_declarations_are_pinned_and_malformed_shapes_owned(self):
        reader = Reader({'uv.lock': '[[package]]\nname="x"\nsource={git="https://github.com/vendor/uv?rev=main#abc"}\n',
                         'poetry.lock': '[[package]]\nname="y"\n[package.source]\ntype="git"\nurl="https://github.com/vendor/poetry.git"\n'})
        result = extract(reader, 1, 'org/source', ['a' * 40])
        self.assertEqual({r['canonical_name'] for r in result.repositories}, {'vendor/uv', 'vendor/poetry'})
        with self.assertRaisesRegex(MirrorError, 'dependency_format_unsupported'):
            extract(Reader({'pyproject.toml': 'project="invalid"'}), 1, 'org/source', ['a' * 40])

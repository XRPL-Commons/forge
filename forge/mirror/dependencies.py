"""Bounded declaration extraction from immutable Git blobs, never a checkout.

The reader supplies list_tree(commit)->[{path,mode,oid}] and read_blob(oid).
Only explicitly supported declaration fields grant access. A package script or
an edited worker manifest cannot extend this closure. Nested submodules return
pinned requests for the host resolver; they never read a mutable remote HEAD.
"""
import configparser
import fnmatch
import json
import posixpath
import re
import tomllib
from dataclasses import dataclass

from .policy import MirrorError, canonical_name, github_repository, OID

MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 512
MAX_ENTRIES = 20000
MAX_DECLARATIONS = 2048
GIT_MARKERS = ('git+', 'git://', 'ssh://', 'git@', 'github:', 'https://github.com/')


@dataclass(frozen=True)
class Extraction:
    repositories: list
    submodules: list
    unsupported: list


def relative_submodule(value, parent):
    if value.startswith(('./', '../')):
        # Git resolves relative URLs against the upstream repository directory.
        name = posixpath.normpath(canonical_name(parent) + '/' + value)
        return canonical_name(name.removesuffix('.git'))
    return github_repository(value)


def _extract(reader, repository_id, repository, commits):
    declarations, submodules, unsupported = [], [], []
    total_bytes, file_count = 0, 0
    if len(commits) > 101 or any(not OID.fullmatch(oid) for oid in commits):
        raise MirrorError('dependency_format_unsupported')
    for commit in sorted(set(commits)):
        entries = reader.list_tree(commit)
        if len(entries) > MAX_ENTRIES:
            raise MirrorError('mirror_capacity')
        tree = {entry['path']: entry for entry in entries}
        evidence = None

        def add(value, *, shorthand=False):
            if not isinstance(value, str):
                return
            candidate = value.strip()
            # PEP508's package @ URL and editable requirement prefix.
            if ' @ ' in candidate:
                candidate = candidate.split(' @ ', 1)[1].split(' ;', 1)[0].strip()
            if candidate.startswith('-e '):
                candidate = candidate[3:].strip()
            if shorthand and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:#[^\s]+)?', candidate):
                candidate = 'github:' + candidate
            if not candidate.startswith(GIT_MARKERS):
                return
            # pip puts a revision after repository.git; it is not part of its name.
            candidate = re.sub(r'(\.git)@[^#]+', r'\1', candidate)
            try:
                name = github_repository(candidate)
            except (MirrorError, ValueError):
                unsupported.append({**evidence, 'reason': 'dependency_format_unsupported'})
                return
            declarations.append({'canonical_name': name, 'source_evidence': dict(evidence)})
            if len(declarations) > MAX_DECLARATIONS:
                raise MirrorError('mirror_capacity')

        def read(path):
            nonlocal total_bytes, file_count, evidence
            entry = tree.get(path)
            if entry is None:
                return None
            if entry['mode'] not in ('100644', '100755'):
                raise MirrorError('dependency_format_unsupported')
            raw = reader.read_blob(entry['oid'])
            total_bytes += len(raw)
            file_count += 1
            if total_bytes > MAX_BYTES or file_count > MAX_FILES:
                raise MirrorError('mirror_capacity')
            evidence = dict(repository_id=repository_id, commit_oid=commit, path=path, blob_oid=entry['oid'])
            try:
                return raw.decode('utf-8')
            except UnicodeError:
                raise MirrorError('dependency_format_unsupported') from None

        def json_file(path):
            raw = read(path)
            if raw is None:
                return None
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, RecursionError):
                raise MirrorError('dependency_format_unsupported') from None

        package = json_file('package.json')
        workspace_paths = []
        if package is not None:
            def package_dependencies(value):
                for key in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
                    values = value.get(key, {})
                    if isinstance(values, dict):
                        for item in values.values():
                            add(item, shorthand=True)
            package_dependencies(package)
            workspaces = package.get('workspaces', [])
            if isinstance(workspaces, dict):
                workspaces = workspaces.get('packages', [])
            if not isinstance(workspaces, list) or len(workspaces) > MAX_FILES:
                raise MirrorError('dependency_format_unsupported')
            for pattern in workspaces:
                if not isinstance(pattern, str) or pattern.startswith('/') or '..' in pattern.split('/'):
                    raise MirrorError('dependency_format_unsupported')
                workspace_paths += [p for p in tree if p.endswith('/package.json') and
                                    fnmatch.fnmatchcase(p, pattern.rstrip('/') + '/package.json')]
            for path in sorted(set(workspace_paths)):
                value = json_file(path)
                package_dependencies(value)

        for path in ('package-lock.json', 'npm-shrinkwrap.json'):
            value = json_file(path)
            if value is None:
                continue
            # npm lock v1's nested dependencies and v2/v3's packages share these
            # named resolution fields; arbitrary script/string fields are ignored.
            def lock_walk(item, depth=0):
                if depth > 100:
                    raise MirrorError('dependency_format_unsupported')
                if not isinstance(item, dict):
                    return
                for key in ('resolved', 'version', 'from'):
                    add(item.get(key), shorthand=True)
                for key in ('dependencies', 'packages'):
                    if isinstance(item.get(key), dict):
                        for child in item[key].values():
                            lock_walk(child, depth + 1)
            lock_walk(value)

        for path in ('pnpm-lock.yaml', 'yarn.lock'):
            raw = read(path)
            if raw is None:
                continue
            # Resolution-only subset of the lock grammar. No YAML evaluation,
            # aliases, custom tags or repository-controlled parser hooks.
            for line in raw.splitlines():
                stripped = line.strip()
                if re.match(r'(?:resolution|resolved|repo|version|specifier)\s*[: ]', stripped):
                    for match in re.finditer(r'(?:git\+https://|git\+ssh://|https://github\.com/|ssh://git@github\.com/|git@github\.com:|github:)[^\s\x22\x27,}]+', stripped):
                        add(match.group())

        raw = read('pyproject.toml')
        if raw is not None:
            try:
                value = tomllib.loads(raw)
            except tomllib.TOMLDecodeError:
                raise MirrorError('dependency_format_unsupported') from None
            project_document = value
            project = value.get('project', {})
            for item in project.get('dependencies', []):
                add(item)
            for group in project.get('optional-dependencies', {}).values():
                for item in group:
                    add(item)
            poetry = value.get('tool', {}).get('poetry', {})
            groups = [poetry.get('dependencies', {}), poetry.get('dev-dependencies', {})]
            groups += [g.get('dependencies', {}) for g in poetry.get('group', {}).values() if isinstance(g, dict)]
            for group in groups:
                for value in group.values():
                    if isinstance(value, dict):
                        add(value.get('git'))
            for source in project_document.get('tool', {}).get('uv', {}).get('sources', {}).values():
                if isinstance(source, dict):
                    add(source.get('git'))

        for lock_path in ('poetry.lock', 'uv.lock'):
            raw = read(lock_path)
            if raw is None:
                continue
            try:
                lock = tomllib.loads(raw)
            except tomllib.TOMLDecodeError:
                raise MirrorError('dependency_format_unsupported') from None
            for package in lock.get('package', []):
                source = package.get('source', {})
                if source.get('type') == 'git':
                    add(source.get('url'))
                git = source.get('git')
                if isinstance(git, str):
                    add(git.split('?', 1)[0])

        requirement_paths = [p for p in tree if re.fullmatch(r'(?:requirements(?:/[^/]+)*|requirements[^/]*)\.(?:txt|in)', p)]
        seen = set()
        while requirement_paths:
            path = requirement_paths.pop(0)
            if path in seen:
                continue
            seen.add(path)
            raw = read(path)
            if raw is None:
                continue
            for line in raw.splitlines():
                line = line.strip()
                include = re.fullmatch(r'(?:-r\s*|--requirement[= ])([^\s]+)', line)
                if include:
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), include[1]))
                    if target.startswith(('/', '../')) or target not in tree:
                        unsupported.append({**evidence, 'reason': 'dependency_format_unsupported'})
                    else:
                        requirement_paths.append(target)
                elif line and not line.startswith('#'):
                    add(line)

        raw = read('.gitmodules')
        if raw is not None:
            config = configparser.ConfigParser(interpolation=None, strict=True)
            try:
                config.read_string(raw)
                for section in config.sections():
                    path, url = config.get(section, 'path'), config.get(section, 'url')
                    entry = tree.get(path)
                    if not section.startswith('submodule ') or entry is None or entry['mode'] != '160000':
                        raise MirrorError('dependency_format_unsupported')
                    name = relative_submodule(url, repository)
                    record = {'canonical_name': name, 'source_evidence': dict(evidence)}
                    declarations.append(record)
                    submodules.append({**record, 'commit_oid': entry['oid'], 'path': path})
            except (configparser.Error, ValueError):
                raise MirrorError('dependency_format_unsupported') from None
    unique = {json.dumps(item, sort_keys=True): item for item in declarations}
    return Extraction(list(unique.values()), submodules, unsupported)


def extract(reader, repository_id, repository, commits):
    try:
        return _extract(reader, repository_id, repository, commits)
    except (AttributeError, KeyError, TypeError, UnicodeError, RecursionError):
        raise MirrorError('dependency_format_unsupported') from None


def submodule_closure(reader, repository_id, repository, commits, *, resolve_pinned,
                      max_depth=5, max_repositories=64):
    """Resolve only exact gitlinks; callback enforces deny/access before fetching.

    resolve_pinned(declaration)->(repository_id, canonical_repository, reader).
    The declaration carries parent blob evidence and its exact gitlink OID.
    General dynamic package-script dependencies remain unsupported.
    """
    result = extract(reader, repository_id, repository, commits)
    declarations, unsupported = list(result.repositories), list(result.unsupported)
    pending = [(item, 1, []) for item in result.submodules]
    submodules, visited = list(result.submodules), set()
    while pending:
        declaration, depth, chain = pending.pop(0)
        identity = (declaration['canonical_name'], declaration['commit_oid'])
        if identity in visited:
            continue
        visited.add(identity)
        if depth > max_depth or len(visited) > max_repositories:
            raise MirrorError('mirror_capacity')
        repo_id, name, nested_reader = resolve_pinned(declaration)
        child = extract(nested_reader, repo_id, name, [declaration['commit_oid']])
        child_chain = [*chain, declaration['source_evidence']]
        for item in child.repositories:
            item['source_evidence']['parent_declarations'] = child_chain
        declarations.extend(child.repositories)
        unsupported.extend(child.unsupported)
        submodules.extend(child.submodules)
        pending.extend((item, depth + 1, child_chain) for item in child.submodules)
        if len(declarations) > MAX_DECLARATIONS:
            raise MirrorError('mirror_capacity')
    return Extraction(declarations, submodules, unsupported)

"""Resolve bounded Git dependency selectors without executing package code."""
import re

from .policy import MirrorError, OID
from .git_store import run_git

VERSION = re.compile(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


def _version(text):
    matched = VERSION.fullmatch(text)
    return tuple(map(int, matched.groups())) if matched else None


def _range(text):
    """Stable npm selectors: exact, ^, ~, x/*, and comparator intersections."""
    text = text.strip()
    if text in ('', '*', 'x', 'X'):
        return lambda version: True
    if text.startswith(('^', '~')):
        mode, body = text[0], text[1:]
        parts = body.removeprefix('v').split('.')
        if not 1 <= len(parts) <= 3 or any(not item.isdigit() for item in parts):
            raise MirrorError('dependency_format_unsupported')
        low = tuple(map(int, parts)) + (0,) * (3 - len(parts))
        if mode == '~':
            high = (low[0] + 1, 0, 0) if len(parts) == 1 else (low[0], low[1] + 1, 0)
        else:
            index = next((i for i, item in enumerate(low) if item), min(len(parts) - 1, 2))
            high = low[:index] + (low[index] + 1,) + (0,) * (2 - index)
        return lambda version: low <= version < high
    match = re.fullmatch(r'v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?', text)
    if match:
        parts = match.groups()
        count = next((i for i, value in enumerate(parts) if value is None or value in ('x', 'X', '*')), 3)
        if any(value is not None and value not in ('x', 'X', '*') for value in parts[count:]):
            raise MirrorError('dependency_format_unsupported')
        prefix = tuple(int(value) for value in parts[:count])
        return lambda version: version[:count] == prefix
    tokens = text.split()
    predicates = []
    for token in tokens:
        matched = re.fullmatch(r'(>=|<=|>|<|=)(v?\d+\.\d+\.\d+)', token)
        if not matched or _version(matched[2]) is None:
            raise MirrorError('dependency_format_unsupported')
        op, value = matched[1], _version(matched[2])
        predicates.append(lambda version, op=op, value=value: {'>=': version >= value, '<=': version <= value,
            '>': version > value, '<': version < value, '=': version == value}[op])
    if not predicates:
        raise MirrorError('dependency_format_unsupported')
    return lambda version: all(predicate(version) for predicate in predicates)


def resolve_revision(git_store, repository, selector=None, *, alive=lambda: True):
    if selector is None or selector in ('', 'HEAD'):
        candidates = ['HEAD']
    elif not isinstance(selector, str) or len(selector) > 256 or any(ord(c) < 32 for c in selector):
        raise MirrorError('dependency_format_unsupported')
    elif OID.fullmatch(selector):
        git_store.ensure(repository, pinned_oids=[selector], alive=alive)
        return selector
    elif selector.startswith('semver:'):
        alternatives = selector[7:].split('||')
        if len(alternatives) > 10:
            raise MirrorError('dependency_format_unsupported')
        predicates = [_range(value) for value in alternatives]
        tags = run_git(['--git-dir', str(git_store.path(repository.repository_id)), 'for-each-ref',
                        '--format=%(refname:strip=2)', 'refs/tags'], limit=1024 * 1024, alive=alive).decode().splitlines()
        choices = [(version, tag) for tag in tags if (version := _version(tag)) is not None
                   and any(predicate(version) for predicate in predicates)]
        if not choices:
            raise MirrorError('revision_unavailable')
        candidates = ['refs/tags/' + max(choices)[1]]
    else:
        if selector.startswith(('refs/heads/', 'refs/tags/')):
            candidates = [selector]
        else:
            candidates = ['refs/heads/' + selector, 'refs/tags/' + selector]
        for candidate in candidates:
            try:
                run_git(['check-ref-format', candidate], alive=alive)
            except MirrorError:
                raise MirrorError('dependency_format_unsupported') from None
    resolved = set()
    for ref in candidates:
        try:
            resolved.add(run_git(['--git-dir', str(git_store.path(repository.repository_id)),
                                  'rev-parse', '--verify', ref + '^{commit}'], alive=alive).decode().strip())
        except MirrorError as error:
            if error.reason != 'mirror_fetch_failed':
                raise
    if not resolved and isinstance(selector, str) and re.fullmatch('[0-9a-f]{4,39}', selector):
        try:
            resolved.add(run_git(['--git-dir', str(git_store.path(repository.repository_id)),
                'rev-parse', '--verify', selector + '^{commit}'], alive=alive).decode().strip())
        except MirrorError as error:
            if error.reason != 'mirror_fetch_failed':
                raise
    # Ambiguous branch/tag selectors never silently choose a different tree than
    # the package tool. The refusal identifies a dependency needing an exact ref.
    if len(resolved) != 1:
        raise MirrorError('revision_unavailable' if not resolved else 'dependency_format_unsupported')
    return resolved.pop()

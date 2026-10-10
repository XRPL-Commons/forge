"""Export the pinned Forge tree locally. Git configuration and credentials stay local."""
import base64
import hashlib
import re
import shutil
import tempfile

from .contracts import SetupError, require
from .github import run_bounded

REPOSITORY = 'https://github.com/XRPL-Commons/forge.git'
PACK_LIMIT = 32 * 1024 * 1024


def export(revision, *, previous=None, replace_initial_source=False, run=run_bounded):
    require(isinstance(revision, str) and re.fullmatch('[a-f0-9]{40}', revision), 'invalid_revision', 'Select a full Forge commit SHA.')
    require(previous is None or isinstance(previous, str) and re.fullmatch('[a-f0-9]{40}', previous),
            'invalid_revision', 'Use a full previous Forge commit SHA.')
    require(type(replace_initial_source) is bool and (not replace_initial_source or previous is not None),
            'invalid_source_replacement', 'Initial source replacement requires the exact installed revision.')
    executable = shutil.which('git')
    require(executable, 'git_missing', 'Install Git locally before setting up Forge.')
    try:
        with tempfile.TemporaryDirectory(prefix='forge-source-') as directory:
            # This disposable checkout must have no writers after a Git command returns.
            git = [executable, '-c', 'maintenance.auto=false', '-C', directory]
            run(git + ['init', '--quiet'])
            run(git + ['remote', 'add', 'origin', REPOSITORY])
            # Local Git's credential helper may use the local GitHub login.
            # No environment values or config files enter the pack. Initial
            # installation needs one tree; an upgrade includes ancestry proof.
            depth = ['--depth=1'] if previous is None else []
            run(git + ['fetch', '--quiet', *depth, 'origin', revision], timeout=180)
            head = run(git + ['rev-parse', 'FETCH_HEAD']).decode().strip()
            require(head == revision, 'source_changed', 'Fetched source differs from the requested commit.')
            if previous is not None and not replace_initial_source:
                try:
                    run(git + ['merge-base', '--is-ancestor', previous, revision])
                except SetupError as error:
                    if error.code == 'github_request_failed':
                        raise SetupError('source_not_forward', 'The installed revision is not a verified ancestor of this release. A never-started setup may use explicit --replace-initial-source; running factories require a forward release.') from None
                    raise
            scope = [revision] if replace_initial_source else ['--no-walk', revision] if previous is None else [revision, '^' + previous]
            objects = run(git + ['rev-list', '--objects', *scope])
            identifiers = [line.split(b' ', 1)[0] for line in objects.splitlines()]
            require(identifiers and all(re.fullmatch(b'[a-f0-9]{40}', item) for item in identifiers), 'invalid_source', 'Git returned an invalid source inventory.')
            pack = run(git + ['pack-objects', '--stdout'], input_bytes=b'\n'.join(identifiers) + b'\n',
                       input_limit=4 * 1024 * 1024, output_limit=PACK_LIMIT, timeout=180)
            require(pack.startswith(b'PACK'), 'invalid_source', 'Git did not produce a valid source pack.')
            return {'sha256': hashlib.sha256(pack).hexdigest(), 'data': base64.b64encode(pack).decode()}
    except SetupError as error:
        if error.code == 'source_not_forward':
            raise
        raise SetupError('source_unavailable', 'Could not export the pinned Forge source locally. Check local GitHub access to XRPL-Commons/forge, then resume; no GitHub credential was sent to the VM.') from None

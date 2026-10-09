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


def export(revision, *, run=run_bounded):
    require(isinstance(revision, str) and re.fullmatch('[a-f0-9]{40}', revision), 'invalid_revision', 'Select a full Forge commit SHA.')
    executable = shutil.which('git')
    require(executable, 'git_missing', 'Install Git locally before setting up Forge.')
    try:
        with tempfile.TemporaryDirectory(prefix='forge-source-') as directory:
            git = [executable, '-C', directory]
            run(git + ['init', '--quiet'])
            run(git + ['remote', 'add', 'origin', REPOSITORY])
            # Local Git's credential helper may use the local GitHub login.
            # No environment values, config files or history enter the pack.
            run(git + ['fetch', '--quiet', '--depth=1', 'origin', revision], timeout=180)
            head = run(git + ['rev-parse', 'FETCH_HEAD']).decode().strip()
            require(head == revision, 'source_changed', 'Fetched source differs from the requested commit.')
            objects = run(git + ['rev-list', '--objects', '--no-walk', revision])
            identifiers = [line.split(b' ', 1)[0] for line in objects.splitlines()]
            require(identifiers and all(re.fullmatch(b'[a-f0-9]{40}', item) for item in identifiers), 'invalid_source', 'Git returned an invalid source inventory.')
            pack = run(git + ['pack-objects', '--stdout'], input_bytes=b'\n'.join(identifiers) + b'\n',
                       input_limit=4 * 1024 * 1024, output_limit=PACK_LIMIT, timeout=180)
            require(pack.startswith(b'PACK'), 'invalid_source', 'Git did not produce a valid source pack.')
            return {'sha256': hashlib.sha256(pack).hexdigest(), 'data': base64.b64encode(pack).decode()}
    except SetupError:
        raise SetupError('source_unavailable', 'Could not export the pinned Forge source locally. Check local GitHub access to XRPL-Commons/forge, then resume; no GitHub credential was sent to the VM.') from None

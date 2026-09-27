"""Run inside the actual Claude Bash sandbox; contains no real credentials."""
import os
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
import sys
import urllib.error
import urllib.request
import runpy


def check_repository_access():
    # The assigned GitHub credential must survive provider-credential scrubbing.
    # Use a synthetic token and a public endpoint; never send a real job token.
    if os.environ.get('GH_TOKEN') != 'forge-synthetic-repository-token':
        raise SystemExit('Assigned repository token was removed')
    try:
        with urllib.request.urlopen('https://api.github.com/meta', timeout=15):
            pass
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403, 429):
            raise SystemExit('GitHub connectivity check failed') from None
    except (OSError, urllib.error.URLError):
        raise SystemExit('GitHub connectivity check failed') from None


def main():
    check_repository_access()
    check_isolation()
    runpy.run_path('/opt/forge/build_environment.py')['check']()
    check_dependency_install(Path('/work/repository'))
    check_git_writes(Path('/work/repository'))
    Path('/work/repository/claude-probe-proof').write_text(sys.argv[1])
    print('forge-canary:' + sys.argv[1])


def check_dependency_install(directory):
    """Exercise real bin links, lifecycle scripts, lockfiles and clean reinstalls.

    Only run in a disposable qualification checkout, never in a worker's repo.
    The fixture is local so registry outages cannot masquerade as sandbox errors.
    """
    fixture = {
        'package/package.json': json.dumps({'name': 'forge-install-fixture', 'version': '1.0.0',
                                           'bin': {'forge-fixture': 'bin.js'}}),
        'package/bin.js': '#!/usr/bin/env node\nconsole.log("forge-bin-ok");\n',
    }
    with tarfile.open(directory / 'fixture.tgz', 'w:gz') as archive:
        for name, content in fixture.items():
            data = content.encode()
            entry = tarfile.TarInfo(name); entry.size = len(data); entry.mode = 0o755
            archive.addfile(entry, io.BytesIO(data))
    (directory / 'package.json').write_text(json.dumps({
        'name': 'forge-install-canary', 'version': '1.0.0', 'private': True,
        'dependencies': {'forge-install-fixture': 'file:fixture.tgz'},
        'scripts': {'postinstall': 'node lifecycle.cjs'},
    }))
    (directory / 'lifecycle.cjs').write_text(
        "for (const k of ['ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','FORGE_PROBE_SECRET']) "
        "if (process.env[k]) throw Error('Provider environment exposed'); "
        "require('fs').writeFileSync('lifecycle-proof', 'ok');\n")
    env = {**os.environ, 'CI': 'true', 'COREPACK_ENABLE_NETWORK': '0'}
    for argv in (['npm', 'install', '--offline', '--no-audit', '--no-fund'],
                 ['npm', 'ci', '--offline', '--no-audit', '--no-fund'],
                 ['pnpm', 'install', '--offline', '--no-frozen-lockfile'],
                 ['pnpm', 'install', '--offline', '--frozen-lockfile']):
        if argv[0] == 'pnpm' and '--no-frozen-lockfile' in argv:
            shutil.rmtree(directory / 'node_modules')
        (directory / 'lifecycle-proof').unlink(missing_ok=True)
        subprocess.run(argv, cwd=directory, env=env, check=True, timeout=45,
                       stdout=subprocess.DEVNULL)
        assert (directory / 'lifecycle-proof').read_text() == 'ok'
        output = subprocess.check_output([str(directory / 'node_modules/.bin/forge-fixture')],
                                         cwd=directory, env=env, text=True, timeout=10)
        assert output.strip() == 'forge-bin-ok'
    print('forge-dependency-install-ok', flush=True)


def check_git_writes(directory):
    subprocess.run(['git', 'add', 'package.json'], cwd=directory, check=True, timeout=10)
    subprocess.run(['git', '-c', 'user.name=Forge canary', '-c', 'user.email=canary@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-m', 'Verify sandbox writes'],
                   cwd=directory, check=True, timeout=10, stdout=subprocess.DEVNULL)
    subprocess.run(['git', 'checkout', '-b', 'forge-canary'], cwd=directory, check=True, timeout=10,
                   stdout=subprocess.DEVNULL)
    print('forge-git-writes-ok', flush=True)


def check_isolation():
    for key in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'FORGE_PROBE_SECRET'):
        if os.environ.get(key):
            raise SystemExit('Provider environment was not isolated')
    for name in ('/work/home/probe-private', '/work/home/.claude/.credentials.json', '/work/home/.codex/auth.json'):
        try:
            Path(name).read_bytes()
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise SystemExit('Private file was readable')
    for name in ('/work/outside-repository', '/opt/forge/claude-control/forbidden'):
        try:
            Path(name).write_text('must not be writable')
        except (PermissionError, OSError):
            pass
        else:
            raise SystemExit('Write escaped repository: ' + name)


if __name__ == '__main__':
    main()

"""Run inside the actual Grok Bash sandbox; contains no real credentials."""
import os
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


def check_isolation():
    for key in ('XAI_API_KEY', 'GROK_DEPLOYMENT_KEY', 'FORGE_PROBE_SECRET'):
        if os.environ.get(key):
            raise SystemExit('Provider environment was not isolated')
    envelope = Path('/work/home/forge-exit.json')
    for operation in (lambda: envelope.read_bytes(), lambda: envelope.write_text('forged'),
                      lambda: envelope.rename('/work/repository/stolen-envelope')):
        try:
            operation()
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise SystemExit('Private execution envelope was accessible')
    for name in ('/work/home/probe-private', '/work/home/.grok/auth.json',
                 '/work/home/.claude/.credentials.json', '/work/home/.codex/auth.json'):
        try:
            Path(name).read_bytes()
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise SystemExit('Private file was readable')
    try:
        Path('/work/outside-repository').write_text('must not be writable')
    except (PermissionError, OSError):
        pass
    else:
        raise SystemExit('Write escaped repository')


def main():
    check_repository_access()
    check_isolation()
    runpy.run_path('/opt/forge/build_environment.py')['check']()
    Path('/work/repository/grok-probe-proof').write_text(sys.argv[1])
    print('forge-canary:' + sys.argv[1])


if __name__ == '__main__':
    main()

"""Run inside the actual Grok Bash sandbox; contains no real credentials."""
import os
from pathlib import Path
import sys
import runpy


def check_repository_access():
    # Publication credentials never enter the worker. Actual mirror routing and
    # network isolation have their own container qualification.
    if any(key in os.environ for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GIT_ASKPASS')):
        raise SystemExit('Unexpected GitHub credential in worker environment')
    if 'http://forge-git/github/' not in Path('/opt/forge/gitconfig').read_text():
        raise SystemExit('Read-only mirror routing is unavailable')


def main():
    check_repository_access()
    check_isolation()
    runpy.run_path('/opt/forge/build_environment.py')['check']()
    Path('/work/repository/grok-probe-proof').write_text(sys.argv[1])
    print('forge-canary:' + sys.argv[1])


if __name__ == '__main__':
    main()

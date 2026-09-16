"""Run inside the actual Claude Bash sandbox; contains no real credentials."""
import os
from pathlib import Path
import sys

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
try:
    Path('/work/outside-repository').write_text('must not be writable')
except (PermissionError, OSError):
    pass
else:
    raise SystemExit('Write escaped repository')
Path('/work/repository/claude-probe-proof').write_text(sys.argv[1])
print('forge-canary:' + sys.argv[1])

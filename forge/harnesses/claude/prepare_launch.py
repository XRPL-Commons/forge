"""Build-time placeholders for Claude's immutable parent working directory.

Scrub mode protects these launch-local files. Pre-create them because bubblewrap
cannot synthesize missing mount targets on the container's read-only image.
No repository or credential data belongs in this directory.
"""
from pathlib import Path


def prepare(root):
    files = ('.gitconfig', '.gitmodules', '.bashrc', '.bash_profile', '.zshrc', '.zprofile',
             '.profile', '.ripgreprc', '.mcp.json', '.npmrc', '.yarnrc', '.yarnrc.yml',
             'bunfig.toml', 'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
             '.env', '.env.local', '.env.development', '.env.development.local',
             '.env.test', '.env.test.local', '.env.production', '.env.production.local',
             '.git/config', '.git/config.lock', '.git/config.worktree', '.git/config.worktree.lock', '.git/info/exclude', '.git/commondir',
             '.claude/settings.json', '.claude/settings.local.json')
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}' if name.endswith('.json') else '')
    for name in ('.vscode', '.idea', '.claude/commands', '.claude/agents', '.git/hooks',
                 '.git/modules', '.git/worktrees', '.git/glab-cli', 'node_modules/.bin', 'scripts', '.github'):
        (root / name).mkdir(parents=True, exist_ok=True)


if __name__ == '__main__':
    prepare(Path('/opt/forge/claude-control'))

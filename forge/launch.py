"""Select the qualified host interpreter environment before opening SQLite."""
import os
from pathlib import Path
import runpy
import sys


def launch(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    script = bool(argv and argv[0] == '--script')
    if script:
        argv.pop(0)
    if not argv:
        raise SystemExit('A Forge module or --script path is required')
    target, arguments = argv[0], argv[1:]
    options = arguments[:arguments.index('--')] if '--' in arguments else arguments
    root = os.environ.get('FORGE_ROOT')
    for index, value in enumerate(options):
        if value == '--root':
            if index + 1 >= len(options) or options[index + 1].startswith('--'):
                raise SystemExit('--root needs a path')
            root = options[index + 1]
        elif value.startswith('--root='):
            root = value.split('=', 1)[1]
        else:
            continue
        if not root:
            raise SystemExit('--root needs a path')
    if not root and target == 'forge.host_setup':
        root = str(Path.home() / '.local/share/forge')
    if root:
        from .store.db import qualified_sqlite
        if not qualified_sqlite():
            from .store.migration.runtime import inspect
            prepared = inspect(Path(root))
            if prepared:
                library = prepared['library_directory']
                if os.environ.get('LD_LIBRARY_PATH') == library:
                    raise SystemExit('The prepared SQLite runtime did not load; run bin/forge setup initialize again')
                environment = {**os.environ, 'LD_LIBRARY_PATH': library}
                command = [sys.executable, '-m', 'forge.launch', *(['--script'] if script else []), target, *arguments]
                os.execve(sys.executable, command, environment)
            elif os.path.lexists(Path(root) / 'state/storage'):
                raise SystemExit('SQLite runtime qualification is required; run bin/forge setup initialize')
        from .store.runtime import SETUP_REQUIRED
        # Refuse an installation with no selected store before startup. Only the
        # selection marker is checked here: opening the store would also refuse
        # a pending schema migration, and the upgrade itself runs through here.
        # Each service validates the store when it opens it.
        if not os.path.lexists(Path(root) / 'state/storage') and target != 'forge.host_setup':
            raise SystemExit(SETUP_REQUIRED)
    sys.argv = [target, *arguments]
    if script:
        runpy.run_path(target, run_name='__main__')
    else:
        if not target.startswith('forge.'):
            raise SystemExit('Only Forge modules may be launched')
        runpy.run_module(target, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    launch()

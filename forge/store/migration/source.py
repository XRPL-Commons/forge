"""Installation-owned files required by the standalone migration tool."""
from pathlib import Path


def source_files(directory):
    directory = Path(directory)
    return sorted({*directory.rglob('*.py'), *directory.rglob('*.sql'),
                   *directory.glob('harnesses/*/manifest.json')})


CLIENT_CONTRACT_FILES = ('forge_client/__init__.py', 'forge_client/contracts.py')


def migration_files(source):
    """Complete standalone runtime plus its two credential-free shared contracts."""
    source=Path(source)
    return sorted([*source_files(source/'forge'), *(source/name for name in CLIENT_CONTRACT_FILES)])


def migration_path(name):
    path=Path(name)
    return (name in CLIENT_CONTRACT_FILES or name.startswith('forge/') and
            (path.suffix in {'.py','.sql'} or path.match('forge/harnesses/*/manifest.json')))

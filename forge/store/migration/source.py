"""Installation-owned files required by the standalone migration tool."""
from pathlib import Path


def source_files(directory):
    directory = Path(directory)
    return sorted({*directory.rglob('*.py'), *directory.rglob('*.sql'),
                   *directory.glob('harnesses/*/manifest.json')})

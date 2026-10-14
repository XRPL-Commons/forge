"""Dispatch the selected, image-owned harness readiness probe."""
import json
from pathlib import Path
import runpy


def main():
    note = json.loads(Path('/run/forge-input/assignment.json').read_text())
    directory = Path('/work/repository'); directory.mkdir(exist_ok=True)
    registry = runpy.run_path('/opt/forge/harnesses/registry.py')
    return registry['adapter'](note.get('provider', 'codex'))['probe'](note, directory)


if __name__ == '__main__':
    raise SystemExit(main())

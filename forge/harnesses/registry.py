"""Versioned adapter contract. Only installation-owned modules can be loaded."""
import json
from pathlib import Path
import re
import runpy

ROOT = Path(__file__).resolve().parent


def manifests():
    result = {}
    for path in sorted(ROOT.glob('*/manifest.json')):
        item = json.loads(path.read_text())
        if (item.get('contract_version') != 1 or item.get('id') != path.parent.name
                or not re.fullmatch(r'[a-z][a-z0-9_-]*', item['id'])
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', item['version'])
                or Path(item['auth_file']).is_absolute() or '..' in Path(item['auth_file']).parts):
            raise ValueError('Invalid installed harness manifest')
        for relative in item.get('login_files', {}):
            if Path(relative).is_absolute() or '..' in Path(relative).parts:
                raise ValueError('Login files must stay inside the private login directory')
        result[item['id']] = item
    return result


def manifest(name):
    try:
        return manifests()[name]
    except KeyError:
        raise ValueError('Unknown installed coding harness') from None


def adapter(name):
    manifest(name)
    module = runpy.run_path(str(ROOT / name / 'adapter.py'))
    if not all(callable(module.get(key)) for key in ('run', 'probe')):
        raise ValueError('Harness must implement run and probe')
    return module

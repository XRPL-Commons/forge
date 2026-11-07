"""Collect license evidence from the exact installed UI build dependency tree.

This deliberately includes build/test packages as a conservative superset of
bundled code. It does not infer licenses or claim that metadata is legal text.
"""
import json
import hashlib
from pathlib import Path

LEGAL_NAMES = ('license', 'licence', 'copying', 'notice', 'copyright')


def write_notices(build, destination, *, runtime=None):
    build, destination = Path(build), Path(destination)
    lock = json.loads((build / 'package-lock.json').read_text())
    supplements = {}
    provenance = build / 'third-party-licenses/provenance.json'
    if provenance.exists():
        for item in json.loads(provenance.read_text()):
            path = provenance.parent / item['file']
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('Supplemental UI license checksum mismatch: ' + item['file'])
            for name, version in item['packages'].items():
                supplements.setdefault(name, []).append((version, item, raw.decode()))
    sections = ['Forge UI third-party notices\n\n'
                'Collected from the locked npm installation used for this build. '
                'Includes build dependencies that may not be shipped at runtime. '
                'Packages retain their own licenses. Registry tarballs and upstream '
                'repositories identify the unchanged upstream sources.\n']
    missing = []
    for relative, locked in sorted(lock['packages'].items()):
        if not relative:
            continue
        package = build / relative
        metadata = package / 'package.json'
        if not metadata.exists():
            # npm omits optional binaries for other platforms.
            if locked.get('optional'):
                continue
            raise ValueError('UI notice dependency was not installed: ' + relative)
        value = json.loads(metadata.read_text())
        if value.get('version') != locked['version']:
            raise ValueError('UI notice dependency differs from its lock: ' + relative)
        name = value['name'] + '@' + value['version']
        sections.append('\n' + '=' * 72 + '\n' + name + '\n')
        for label, datum in [('License metadata', value.get('license', locked.get('license', 'unspecified'))),
                             ('Repository', value.get('repository', 'unspecified')),
                             ('Package source', locked.get('resolved', 'unspecified')),
                             ('Integrity', locked.get('integrity', 'unspecified'))]:
            sections.append(label + ': ' + (datum if isinstance(datum, str) else json.dumps(datum, sort_keys=True)) + '\n')
        files = sorted(path for path in package.rglob('*')
                       if path.is_file() and 'node_modules' not in path.relative_to(package).parts
                       and path.name.lower().startswith(LEGAL_NAMES))
        extra = supplements.get(value['name'], [])
        for version, item, text in extra:
            if version != value['version']:
                raise ValueError('Supplemental UI license needs review for ' + name)
            sections.append('\nSupplement source: ' + item['source'] + '\nSHA-256: ' + item['sha256']
                            + '\n' + item.get('version_evidence', '') + '\n' + text.rstrip() + '\n')
        if not files and not extra:
            if runtime is not None and (Path(runtime) / value['name'] / 'package.json').exists():
                raise ValueError('Redistributed server package has no license text: ' + name)
            missing.append(name)
            sections.append('No license text in package; inspect upstream license before redistribution.\n')
            files = sorted(path for path in package.iterdir()
                           if path.is_file() and path.name.lower().startswith('readme'))
        # Icon sets sometimes carry attribution/licensing in structured metadata.
        if value['name'].startswith('@iconify-json/') and (package / 'info.json').is_file():
            files.append(package / 'info.json')
        for path in files:
            sections.append('\n--- ' + str(path.relative_to(package)) + ' ---\n')
            sections.append(path.read_text(errors='replace').rstrip() + '\n')
    if missing:
        sections.insert(1, '\nPackages without separate license text (upstream review required):\n'
                        + '\n'.join(missing) + '\n')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(''.join(sections))
    return missing

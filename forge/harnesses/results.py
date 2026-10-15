"""One result-file boundary shared by installed adapters and host capture."""
import json
from pathlib import Path
import runpy

SCHEMA = runpy.run_path(str(Path(__file__).resolve().with_name('schema.py')))


def result_limit(note):
    triage = note.get('role') == 'triage' or (note.get('role') == 'steward' and note.get('mode') == 'triage')
    return 4 * 1048576 if triage else 65536


def publish_result(note, output, schema=None):
    """Validate a complete provider file before the atomic supervisor handoff."""
    output = Path(output)
    if not output.is_file():
        return False
    if output.stat().st_size > result_limit(note):
        raise ValueError('Result exceeded its size limit')
    result = json.loads(output.read_text())
    if schema is not None:
        SCHEMA['schema_check'](result, schema)
    output.replace(output.parent / 'result.json')
    return True


def write_result(note, result, directory='/work'):
    encoded = json.dumps(result)
    if len(encoded.encode()) > result_limit(note):
        raise ValueError('Result exceeded its size limit')
    output = Path(directory) / 'final-result.json'
    output.write_text(encoded)
    publish_result(note, output)

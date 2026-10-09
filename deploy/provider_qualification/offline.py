"""Run inside a disposable, offline container. No provider credentials are used."""
import json
from pathlib import Path
import subprocess

QUALIFICATION_HOME = Path('/tmp/qualification-home')
QUALIFICATION_HOME.mkdir()
WORK = Path('/tmp/qualification-work')
WORK.mkdir()
ENV = {'HOME': str(QUALIFICATION_HOME), 'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}


def call(binary, *args, stdin=None):
    return subprocess.run(['/qualification/' + binary, *args], input=stdin,
                          env=ENV, cwd=WORK, text=True, capture_output=True,
                          check=True, timeout=30).stdout


def require_flags(text, flags):
    for flag in flags:
        if flag not in text:
            raise ValueError('Pinned CLI no longer exposes ' + flag)


def main():
    pins = json.loads(Path('/qualification/pins.json').read_text())
    opencode_version = call('opencode', '--version').strip()
    assert opencode_version == 'opencode v' + pins['opencode']['version']
    require_flags(call('opencode', 'run', '--help'), ['--standalone', '--format', '--model', '--file'])
    require_flags(call('opencode', 'auth', 'import', '--help'), ['--standalone'])
    require_flags(call('opencode', 'auth', 'export', '--help'), ['--standalone'])
    credential = [{'id': 'cred_forge_offline_fixture', 'integrationID': 'anthropic',
                   'label': 'Forge synthetic fixture', 'active': True,
                   'value': {'type': 'key', 'key': 'forge-synthetic-not-a-real-api-key'}}]
    call('opencode', 'auth', 'import', '--standalone', stdin=json.dumps(credential))
    assert json.loads(call('opencode', 'auth', 'export', '--standalone', 'anthropic')) == credential

    muse_version = call('muse', '--version').strip()
    assert '(' + pins['muse']['version'] + ')' in muse_version
    require_flags(call('muse', 'exec', '--help'), ['--json', '--prompt-file', '--output-schema',
                  '--approval-mode', '--user-input-auto-resolve', '--max-model-steps'])
    require_flags(call('muse', 'auth', '--help'), ['--api-key-stdin'])
    call('muse', 'schema', 'generate-json-schema', '--out', '/tmp/muse-schema')
    fingerprint = json.loads(Path('/tmp/muse-schema/manifest.json').read_text())['fingerprint']
    assert fingerprint == pins['muse']['schema_fingerprint']
    # Echo is Muse's built-in synthetic provider, never Meta API or a subscription.
    transcript = call('muse', 'exec', '--provider', 'echo', '--json', '--workspace', str(WORK),
                      '--approval-mode', 'never', '--user-input-auto-resolve',
                      '--max-model-steps', '1', 'Forge offline protocol fixture')
    events = [json.loads(line) for line in transcript.splitlines() if line]
    terminal = [event for event in events if event.get('payload_type') == 'run.terminal.completed']
    assert len(terminal) == 1
    assert terminal[0]['schema_version'] == 1
    assert terminal[0]['payload']['text'] == 'echo: Forge offline protocol fixture'
    assert terminal[0]['payload']['terminal'] == 'completed'
    print(json.dumps({'opencode': {'version': opencode_version, 'synthetic_auth_roundtrip': True},
                      'muse': {'version': muse_version, 'schema_fingerprint': fingerprint,
                               'echo_events': len(events), 'echo_completed': True},
                      'model_requests': 0, 'native_isolation_qualified': False,
                      'ready_for_dispatch': False}))


if __name__ == '__main__':
    main()

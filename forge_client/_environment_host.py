"""Installation-bound environment RPC; no credentials or arbitrary host paths."""
import json
import sys

if globals().get('__package__'):
    from ._install_host import InstallError, need, read, validate_intent, service, SOURCE, ROOT, require_setup_entrypoint
else:
    from forge_install_helpers import InstallError, need, read, validate_intent, service, SOURCE, ROOT, require_setup_entrypoint


def dispatch(request):
    need(isinstance(request, dict) and set(request) == {'version', 'intent', 'apply'}, 'invalid_request')
    need(request['version'] == 1 and type(request['apply']) is bool, 'invalid_request')
    validate_intent(request['intent'])
    installed = read()
    need(installed and installed['intent'] == request['intent'], 'installation_conflict')
    need(installed['step'] == 'complete', 'installation_required')
    require_setup_entrypoint(SOURCE, 'forge.install_environment')
    command = ['/usr/bin/env', '-C', str(SOURCE), '/usr/bin/python3', '-m', 'forge.launch', 'forge.install_environment',
               '--root', str(ROOT), '--setup-id', request['intent']['id']]
    if request['apply']:
        command.append('--apply')
    result = service(command, capture=True)
    need(len(result) <= 16384, 'factory_setup_failed')
    return {'ok': True, 'result': json.loads(result)}


def main(payload=None):
    try:
        raw = sys.stdin.buffer.read(4097)
        need(len(raw) <= 4096, 'invalid_request')
        print(json.dumps(dispatch(json.loads(raw)), allow_nan=False))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'factory_setup_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())

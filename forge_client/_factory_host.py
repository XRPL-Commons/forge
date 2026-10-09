"""Installed-factory setup RPC: fixed service-account command, bounded stdin/stdout."""
import json
import pwd
import subprocess
import sys

if globals().get('__package__'):
    from ._install_host import ENV, HOME, ROOT, SOURCE, InstallError, need, read, facts, validate_intent
else:
    from forge_install_helpers import ENV, HOME, ROOT, SOURCE, InstallError, need, read, facts, validate_intent


def dispatch(request):
    need(isinstance(request, dict) and set(request) == {'version', 'intent', 'setup'}, 'invalid_request')
    need(request['version'] == 1, 'invalid_request')
    validate_intent(request['intent']); facts()
    installed = read()
    need(installed and installed['intent'] == request['intent'], 'installation_conflict')
    need(installed['step'] == 'complete', 'installation_required')
    value = request['setup']
    need(isinstance(value, dict) and value.get('setup_id') == request['intent']['id'], 'invalid_request')
    account = pwd.getpwnam('forge')
    need(account.pw_uid > 0 and account.pw_dir == str(HOME) and HOME.resolve() == HOME
         and HOME.stat().st_uid == account.pw_uid, 'existing_installation')
    args = ['runuser', '-u', 'forge', '--', 'env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME=' + str(HOME), 'LANG=C.UTF-8', 'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid),
            '/usr/bin/python3', '-m', 'forge.launch', 'forge.installation_setup', '--root', str(ROOT)]
    result = subprocess.run(args, input=json.dumps(value).encode(), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=SOURCE, env=ENV, timeout=240)
    need(result.returncode in (0, 1) and len(result.stdout) <= 262144, 'factory_setup_failed')
    reply = json.loads(result.stdout)
    need(isinstance(reply, dict) and reply.get('ok') in (True, False), 'factory_setup_failed')
    return reply


def main(payload=None):
    try:
        raw = sys.stdin.buffer.read(65537)
        need(len(raw) <= 65536, 'invalid_request')
        result = dispatch(json.loads(raw))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get('ok') is True else 1
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'factory_setup_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())

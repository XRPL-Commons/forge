"""Run the existing identity bootstrap as the installed Forge service account."""
import json
import os
import pwd
import subprocess
import sys

if globals().get('__package__'):
    from ._install_host import ENV, HOME, ROOT, SOURCE, InstallError, need, read, facts, validate_intent
else:
    from forge_install_helpers import ENV, HOME, ROOT, SOURCE, InstallError, need, read, facts, validate_intent


def identity_dispatch(request):
    need(isinstance(request, dict) and set(request) == {'version', 'intent', 'bootstrap'}, 'invalid_request')
    need(type(request['version']) is int and request['version'] == 1, 'invalid_request')
    validate_intent(request['intent'])
    facts()
    installation = read()
    need(installation and installation['intent'] == request['intent'], 'installation_conflict')
    need(installation['step'] == 'complete', 'installation_required')
    value = request['bootstrap']
    need(isinstance(value, dict) and set(value) == {'version', 'action', 'target', 'revision', 'secret'}, 'invalid_request')
    account = pwd.getpwnam('forge')
    need(account.pw_uid > 0 and account.pw_dir == str(HOME) and HOME.resolve() == HOME
         and HOME.stat().st_uid == account.pw_uid, 'existing_installation')
    args = ['runuser', '-u', 'forge', '--', 'env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME=' + str(HOME), 'LANG=C.UTF-8', 'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid),
            '/usr/bin/python3', '-m', 'forge.operator_bootstrap', '--root', str(ROOT)]
    result = subprocess.run(args, input=json.dumps(value).encode(), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=SOURCE, env=ENV, timeout=420)
    need(result.returncode in (0, 1) and len(result.stdout) <= 16384, 'bootstrap_failed')
    reply = json.loads(result.stdout)
    need(isinstance(reply, dict), 'bootstrap_failed')
    if reply.get('ok') is not True:
        allowed = {'invalid_root', 'installation_required', 'reset_in_progress', 'tailscale_origin_mismatch',
                   'https_conflict', 'identity_conflict', 'plan_changed', 'identity_app_required', 'identity_secret_required',
                   'bootstrap_busy', 'host_unavailable', 'host_update_failed', 'https_setup_failed', 'https_unavailable',
                   'bootstrap_failed', 'invalid_request'}
        code = reply.get('error')
        return {'ok': False, 'error': code if isinstance(code, str) and code in allowed else 'bootstrap_failed'}
    need(set(reply) == {'ok', 'status', 'origin', 'client_id', 'operators', 'generation', 'configured', 'revision'}, 'bootstrap_failed')
    need(not value.get('secret') or value['secret'] not in json.dumps(reply), 'bootstrap_failed')
    return reply  # The client applies the shared bootstrap value validator too.


def identity_main(payload=None):
    try:
        raw = sys.stdin.buffer.read(16385)
        need(len(raw) <= 16384, 'invalid_request')
        result = identity_dispatch(json.loads(raw))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get('ok') is True else 1
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'bootstrap_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


main = identity_main
if __name__ == '__main__':
    sys.exit(identity_main())

"""Execute a supplied maintenance command as the explicitly selected Unix user."""
import os
from pathlib import Path
import pwd
import re
import sys


HOST_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


def launch(user, command):
    if not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user) or not command:
        raise ValueError('invalid service account request')
    account = pwd.getpwnam(user)
    home = Path(account.pw_dir)
    if (not home.is_absolute() or home.resolve() != home or not home.is_dir()
            or home.stat().st_uid != account.pw_uid):
        raise ValueError('service account home must be owned by that account')
    # Resolve the executable through the fixed host PATH, never the SSH account's
    # environment. The private tool and its files are created after switching UID.
    environment = {'PATH': HOST_PATH, 'HOME': str(home),
                   'LANG': 'C.UTF-8', 'XDG_RUNTIME_DIR': '/run/user/' + str(account.pw_uid)}
    if os.geteuid() == account.pw_uid:
        os.chdir(home)
        os.execvpe(command[0], command, environment)
    else:
        os.chdir('/')
        arguments = ['runuser', '-u', user, '--', 'env', '-i', '--chdir=' + str(home),
                     *[key + '=' + value for key, value in environment.items()], *command]
        if os.geteuid() != 0:
            arguments = ['sudo', '-n', '--', *arguments]
        os.execvpe(arguments[0], arguments, {'PATH': environment['PATH'], 'LANG': environment['LANG']})


def main():
    try:
        launch(sys.argv[1], sys.argv[2:])
    except (KeyError, IndexError, OSError, ValueError):
        # No account database content, filesystem exception text or command data.
        raise SystemExit('The selected service account could not run maintenance.') from None


if __name__ == '__main__':
    main()

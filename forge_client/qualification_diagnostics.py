"""Export private qualification evidence over SSH; stdout carries metadata only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re

from .contracts import SetupError, require
from .install import Installation

MAX_RECORD = 96 * 1024
HOST = '''import json,pwd,re,subprocess,sys
from pathlib import Path
def main(payload):
    value=json.loads(sys.stdin.buffer.read(4097))
    if (set(value)!={'version','id'} or value['version']!=1
        or not isinstance(value['id'],str) or not re.fullmatch('[0-9a-f]{32}',value['id'])):
        print(json.dumps({'ok':False,'error':'invalid_request'}));return 1
    account=pwd.getpwnam('forge');source=Path('/home/forge/forge')
    if account.pw_uid<=0 or account.pw_dir!='/home/forge' or source.is_symlink() or source.stat().st_uid!=account.pw_uid:
        print(json.dumps({'ok':False,'error':'existing_installation'}));return 1
    environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}
    args=['runuser','-u','forge','--','env','-i',*['%s=%s'%(k,v) for k,v in environment.items()],
        'HOME=/home/forge','/usr/bin/python3','-m','forge.launch','forge.qualification_diagnostics',
        '--root','/home/forge/.local/share/forge','--id',value['id']]
    result=subprocess.run(args,cwd=source,capture_output=True,timeout=30,env=environment)
    if result.returncode!=0 or len(result.stdout)>132000:
        print(json.dumps({'ok':False,'error':'host_command_failed'}));return 1
    try:report=json.loads(result.stdout)
    except ValueError:
        print(json.dumps({'ok':False,'error':'host_command_failed'}));return 1
    if not isinstance(report,dict) or set(report)!={'ok','content'} or report['ok'] is not True:
        print(json.dumps({'ok':False,'error':'host_command_failed'}));return 1
    print(json.dumps(report));return 0
'''


def execute(args, *, installation=None):
    require(isinstance(args.id, str) and re.fullmatch('[0-9a-f]{32}', args.id), 'invalid_request')
    require(isinstance(args.output, str) and args.output and args.output != '-', 'invalid_request',
            'Choose a new local file with --output; diagnostics are never printed.')
    target = Path(args.output).absolute()
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise SetupError('diagnostic_output_unavailable', 'Choose a new writable output file; existing files and links are not replaced.') from None
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            installation = installation or Installation(args.ssh)
            installation.host.fingerprint = (installation.journal.read() or {}).get('host_key')
            response = installation.host.rpc(HOST, dict(version=1, id=args.id), timeout=60)
            content = response.get('content')
            require(isinstance(content, str) and len(content) <= 131072, 'invalid_host_response')
            try:
                data = base64.b64decode(content, validate=True)
                record = json.loads(data)
            except (ValueError, UnicodeError):
                raise SetupError('invalid_host_response', 'The host returned an invalid private diagnostic.') from None
            require(len(data) <= MAX_RECORD and isinstance(record, dict) and record.get('version') == 1
                    and record.get('diagnostic_id') == args.id, 'invalid_host_response')
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return dict(status='exported', diagnostic_id=args.id, output=str(target),
                bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

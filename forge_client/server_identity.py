"""Run repeatable identity migration steps on an existing Forge installation."""
import getpass
import json
import re
import subprocess
import sys

from .bootstrap import Host
from .contracts import require
from .migration import service_user, transport

HOST = '''import json,os,pwd,subprocess,sys
from pathlib import Path
value=json.loads(sys.stdin.buffer.read(32769))
source=Path(value['source']);root=Path(value['root']);account=pwd.getpwuid(os.geteuid())
if (account.pw_uid<=0 or account.pw_name!=value['service_user'] or
    any(not p.is_absolute() or not p.is_dir() or p.resolve()!=p or p.stat().st_uid!=account.pw_uid for p in (source,root))):
 print(json.dumps({'ok':False,'error':'existing_installation_required'}));sys.exit(0)
if not (source/'forge/identity_migration.py').is_file():
 print(json.dumps({'ok':False,'error':'identity_migration_not_installed'}));sys.exit(0)
command=['/usr/bin/python3','-m','forge.launch','forge.identity_migration',value['step'],'--root',str(root)]
if value.get('revision'):command+=['--revision',value['revision']]
if value['step']=='tailscale':command+=['--mode',value['mode']]
result=subprocess.run(command,cwd=source,input=(value.get('secret') or '').encode(),capture_output=True,timeout=300,
 env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':account.pw_dir,'LANG':'C.UTF-8'})
try:
 if len(result.stdout)>65536:raise ValueError()
 report=json.loads(result.stdout)
 if not isinstance(report,dict) or type(report.get('ok')) is not bool:raise ValueError()
except (ValueError,UnicodeDecodeError):report={'ok':False,'error':'identity_migration_unconfirmed'}
print(json.dumps(report))
'''


def execute(args, *, run=subprocess.run, stdin=None, prompt=None):
    Host(args.ssh,args.source,args.root)
    user=service_user(args)
    require(user is not None,'invalid_service_user','Select the installed service account with --service-user.')
    step=args.identity_command
    require(step in ('plan','apps','sign-in','tailscale'),'invalid_identity_step')
    require(step!='apps' or isinstance(args.revision,str) and re.fullmatch('[a-f0-9]{64}',args.revision),
            'identity_revision_required','Read forge server identity plan, then use its --revision for the apps step.')
    require(not args.secret_stdin or step=='sign-in','unexpected_secret_input','--secret-stdin is only for sign-in.')
    secret=None
    if step=='sign-in':
        stdin=stdin or sys.stdin
        if args.secret_stdin:
            secret=stdin.read(4097).strip()
        else:
            require(stdin.isatty(),'identity_secret_required','Use --secret-stdin for automation; keep the secret out of command arguments.')
            secret=(prompt or getpass.getpass)('Forge GitHub App client secret: ').strip()
        require(isinstance(secret,str) and 0<len(secret)<=4096 and not any(c.isspace() for c in secret),
                'invalid_identity_secret','Supply one GitHub App client secret.')
    payload=dict(source=args.source,root=args.root,service_user=user,step=step,
                 revision=args.revision,mode=args.mode,secret=secret)
    try:
        result=run(transport(args,['python3','-I','-c',HOST]),input=json.dumps(payload).encode(),capture_output=True,timeout=330)
    except (OSError,subprocess.TimeoutExpired):
        require(False,'identity_host_unavailable','The SSH call did not finish. Read the identity plan before resuming the same step.')
    require(result.returncode==0 and len(result.stdout)<=65536,'identity_host_unavailable',
            'Could not run identity migration as the selected service account. Check SSH access and the installed release.')
    try:report=json.loads(result.stdout)
    except (ValueError,UnicodeDecodeError):require(False,'invalid_host_response')
    require(isinstance(report,dict) and type(report.get('ok')) is bool,'invalid_host_response')
    # Host errors contain fixed diagnostics. Never echo stderr or the input secret.
    code=report.get('error','migration_failed')
    require(report['ok'], 'identity_'+code if isinstance(code,str) and re.fullmatch('[a-z_]{1,80}',code) else 'identity_migration_failed',
            'Identity migration did not complete. Read forge server identity plan and resume the selected step.')
    return report

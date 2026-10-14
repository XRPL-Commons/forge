"""Explicit native canary through the installed Forge service account."""
import re
from .contracts import require
from .install import Installation

HOST = '''import json,os,pwd,re,subprocess,sys
from pathlib import Path
def main(payload):
    value=json.loads(sys.stdin.buffer.read(4097))
    if set(value)!={'version','image'} or value['version']!=1 or not re.fullmatch(r'sha256:[0-9a-f]{64}',value['image']):
        print(json.dumps({'ok':False,'error':'invalid_request'}));return 1
    account=pwd.getpwnam('forge');source=Path('/home/forge/forge')
    if account.pw_uid<=0 or account.pw_dir!='/home/forge' or source.is_symlink() or source.stat().st_uid!=account.pw_uid:
        print(json.dumps({'ok':False,'error':'existing_installation'}));return 1
    args=['runuser','-u','forge','--','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin',
          'HOME=/home/forge','LANG=C.UTF-8','/usr/bin/python3','-m','forge.worker_qualification','--image',value['image']]
    result=subprocess.run(args,cwd=source,capture_output=True,timeout=360,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'})
    if result.returncode not in (0,1) or len(result.stdout)>65536:
        print(json.dumps({'ok':False,'error':'factory_setup_failed'}));return 1
    try:report=json.loads(result.stdout)
    except ValueError:
        print(json.dumps({'ok':False,'error':'factory_setup_failed'}));return 1
    print(json.dumps({'ok':True,'result':report}));return 0
'''


def execute(args, *, installation=None):
    require(isinstance(args.image,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',args.image),
            'invalid_image', 'Use the full sha256 image ID built by Forge on this host.')
    if not args.apply:
        return dict(status='planned', image=args.image, ssh=args.ssh,
                    checks=['exact image and engine', 'read-only Git clone/fetch', 'provider TLS',
                            'public npm/pip downloads', 'GitHub, host and cross-job denial',
                            'read-only assignment', 'bounded volume', 'stopped capture', 'cleanup'],
                    next_action='Use --apply to run disposable canaries on the selected host; the installed module and image must already match this release.')
    installation=installation or Installation(args.ssh)
    installation.host.fingerprint=(installation.journal.read() or {}).get('host_key')
    reply=installation.host.rpc(HOST,dict(version=1,image=args.image),timeout=390)
    report=reply.get('result')
    require(isinstance(report,dict), 'invalid_host_response')
    require(report.get('status')=='qualified' and report.get('cleanup')=='confirmed',
            'boundary_qualification_failed', report.get('reason','The native worker boundary did not qualify.'))
    return report

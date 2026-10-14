"""Explicit native canary through the installed Forge service account."""
import re
from .contracts import require
from .install import Installation

# Runs as the service account and works before the new canary module is installed.
# Read only selected SQL configuration; no stale legacy configuration fallback.
INSPECT = '''import json,pathlib,sqlite3,subprocess
root=pathlib.Path('/home/forge/.local/share/forge')
marker=(root/'state/storage').read_text().splitlines()
if len(marker)!=3 or marker[0]!='forge-sqlite-v1' or not pathlib.Path(marker[1]).is_absolute():
    raise ValueError('invalid storage selection')
with sqlite3.connect(pathlib.Path(marker[1],'state/forge.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
    active=db.execute("SELECT enabled FROM runtime_controls WHERE name='factory_active'").fetchone()
    domain='runtime.json' if active and active[0] else 'setup/runtime.json'
    row=db.execute("SELECT settings_json FROM configuration WHERE domain=? AND subject='instance' AND active=1",(domain,)).fetchone()
    if row is None:
        row=db.execute("SELECT settings_json FROM configuration WHERE domain='host-runtime.json' AND subject='instance' AND active=1").fetchone()
    image=json.loads(row[0])['image'] if row else None
revision=subprocess.run(['git','-C','/home/forge/forge','rev-parse','HEAD'],capture_output=True,text=True,check=True,timeout=10).stdout.strip()
engine=subprocess.run(['docker','--host','unix:///var/run/docker.sock','version','--format','{{.Server.Version}}'],capture_output=True,text=True,check=True,timeout=20).stdout.strip()
print(json.dumps(dict(image=image,source_revision=revision,engine_version=engine)))
'''

HOST = '''import json,pwd,re,subprocess,sys
from pathlib import Path
def main(payload):
    value=json.loads(sys.stdin.buffer.read(4097))
    if (set(value)!={'version','image','apply'} or value['version']!=1 or type(value['apply']) is not bool
            or value['image'] is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}',value['image'])):
        print(json.dumps({'ok':False,'error':'invalid_request'}));return 1
    account=pwd.getpwnam('forge');source=Path('/home/forge/forge')
    if account.pw_uid<=0 or account.pw_dir!='/home/forge' or source.is_symlink() or source.stat().st_uid!=account.pw_uid:
        print(json.dumps({'ok':False,'error':'existing_installation'}));return 1
    prefix=['runuser','-u','forge','--','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin','HOME=/home/forge','LANG=C.UTF-8']
    environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}
    inspected=subprocess.run(prefix+['/usr/bin/python3','-c',INSPECT],cwd=source,capture_output=True,text=True,timeout=45,env=environment)
    if inspected.returncode or len(inspected.stdout)>4096:
        print(json.dumps({'ok':False,'error':'factory_setup_failed'}));return 1
    observed=json.loads(inspected.stdout);image=value['image'] or observed['image']
    if not isinstance(image,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
        print(json.dumps({'ok':False,'error':'factory_setup_failed'}));return 1
    identity=dict(image=image,source_revision=observed['source_revision'],engine_version=observed['engine_version'],
                  image_selection='explicit' if value['image'] else 'installed')
    if not value['apply']:
        print(json.dumps({'ok':True,'result':dict(status='planned',**identity)}));return 0
    args=prefix+['/usr/bin/python3','-m','forge.worker_qualification','--image',image]
    result=subprocess.run(args,cwd=source,capture_output=True,timeout=360,env=environment)
    if result.returncode not in (0,1) or len(result.stdout)>65536:
        print(json.dumps({'ok':False,'error':'factory_setup_failed'}));return 1
    try:report=json.loads(result.stdout)
    except ValueError:
        print(json.dumps({'ok':False,'error':'factory_setup_failed'}));return 1
    print(json.dumps({'ok':True,'result':{**report,**identity}}));return 0
'''
HOST = 'INSPECT = ' + repr(INSPECT) + '\n' + HOST

CHECKS = ['exact image and engine', 'read-only Git clone/fetch', 'provider TLS',
          'public npm/pip downloads', 'GitHub, host and cross-job denial',
          'read-only assignment', 'bounded volume', 'stopped capture', 'cleanup']


def execute(args, *, installation=None):
    require(args.image is None or isinstance(args.image,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',args.image),
            'invalid_image', 'Use the full sha256 image ID, or omit --image to select the installed worker image.')
    if not args.apply and args.image:
        report=dict(status='planned',image=args.image,image_selection='explicit')
    else:
        installation=installation or Installation(args.ssh)
        installation.host.fingerprint=(installation.journal.read() or {}).get('host_key')
        reply=installation.host.rpc(HOST,dict(version=1,image=args.image,apply=args.apply),timeout=390 if args.apply else 60)
        report=reply.get('result')
        require(isinstance(report,dict), 'invalid_host_response')
        require(isinstance(report.get('image'),str) and re.fullmatch(r'sha256:[0-9a-f]{64}',report['image'])
                and (args.image is None or report['image']==args.image),'invalid_host_response')
    if args.apply:
        require(report.get('status')=='qualified' and report.get('cleanup')=='confirmed',
                'boundary_qualification_failed', report.get('reason','The native worker boundary did not qualify.'))
    else:
        require(report.get('status')=='planned','invalid_host_response')
        report.update(checks=CHECKS,ssh=args.ssh,
                      next_action='Use --apply to run disposable canaries on the selected host; the installed module and image must already match this release.')
    return report

"""Bounded seven-role acceptance on an installed, idle host."""
import re
from .contracts import require
from .install import Installation

HOST = '''import json,pwd,re,subprocess,sys
from pathlib import Path
def main(payload):
    value=json.loads(sys.stdin.buffer.read(4097))
    if (set(value)!={'version','repo','issue','apply'} or value['version']!=1 or type(value['apply']) is not bool
        or type(value['issue']) is not int or not 1<=value['issue']<=2147483647
        or not isinstance(value['repo'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['repo'])):
        print(json.dumps({'ok':False,'error':'invalid_request'}));return 1
    account=pwd.getpwnam('forge');source=Path('/home/forge/forge')
    if account.pw_uid<=0 or account.pw_dir!='/home/forge' or source.is_symlink() or source.stat().st_uid!=account.pw_uid:
        print(json.dumps({'ok':False,'error':'existing_installation'}));return 1
    environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}
    args=['runuser','-u','forge','--','env','-i',*['%s=%s'%(k,v) for k,v in environment.items()],
        'HOME=/home/forge','/usr/bin/python3','-m','forge.launch','forge.role_qualification',
        '--root','/home/forge/.local/share/forge','--repo',value['repo'],'--issue',str(value['issue'])]
    if value['apply']:args.append('--apply')
    result=subprocess.run(args,cwd=source,capture_output=True,timeout=1800 if value['apply'] else 60,env=environment)
    if result.returncode not in (0,1) or len(result.stdout)>65536:
        print(json.dumps({'ok':False,'error':'role_qualification_failed'}));return 1
    try:report=json.loads(result.stdout)
    except ValueError:
        print(json.dumps({'ok':False,'error':'role_qualification_failed'}));return 1
    print(json.dumps({'ok':True,'result':report}));return 0
'''


def execute(args, *, installation=None):
    require(isinstance(args.repo,str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',args.repo)
            and type(args.issue) is int and 1<=args.issue<=2147483647,'invalid_request')
    installation=installation or Installation(args.ssh)
    installation.host.fingerprint=(installation.journal.read() or {}).get('host_key')
    response=installation.host.rpc(HOST,dict(version=1,repo=args.repo,issue=args.issue,apply=args.apply),
                                   timeout=1830 if args.apply else 90)
    report=response.get('result')
    require(isinstance(report,dict),'invalid_host_response')
    require(report.get('status')==('qualified' if args.apply else 'planned'),
            'role_qualification_failed',report.get('reason','Role qualification did not complete.'))
    if args.apply:
        require(report.get('cleanup')=='confirmed' and report.get('automation_after')=='disabled',
                'role_qualification_failed','Cleanup and unchanged automation must both be confirmed.')
    else:
        report['next_action']='Use --apply to run seven bounded provider invocations. The factory must remain disabled and idle.'
    return report

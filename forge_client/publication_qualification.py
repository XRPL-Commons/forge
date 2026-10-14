"""Supported transport for a host-owned scratch signing qualification."""
import re
from .contracts import NAME, require
from .install import Installation

HOST = '''import json,pwd,subprocess,sys
from pathlib import Path
def main(payload):
    value=json.loads(sys.stdin.buffer.read(8193))
    account=pwd.getpwnam('forge');source=Path('/home/forge/forge')
    if account.pw_uid<=0 or account.pw_dir!='/home/forge' or source.is_symlink() or source.stat().st_uid!=account.pw_uid:
        print(json.dumps({'ok':False,'error':'existing_installation'}));return 1
    result=subprocess.run(['runuser','-u','forge','--','env','-i','PATH=/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME=/home/forge','LANG=C.UTF-8','/usr/bin/python3','-m','forge.launch','forge.publication_qualification',
        '--root','/home/forge/.local/share/forge'],input=json.dumps(value).encode(),cwd=source,
        capture_output=True,timeout=300,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'})
    if result.returncode or len(result.stdout)>65536:
        print(json.dumps({'ok':False,'error':'publication_qualification_unconfirmed'}));return 1
    print(json.dumps({'ok':True,'result':json.loads(result.stdout)}));return 0
'''


def execute(args, *, installation=None):
    require(NAME.fullmatch(args.repo or '') and NAME.fullmatch(args.probe_repo or '') and args.repo != args.probe_repo,
            'invalid_repository', 'Choose the connected target and a different existing private scratch repository.')
    require(args.receipt is None or re.fullmatch('[a-f0-9]{32}', args.receipt), 'invalid_receipt')
    for value in (args.target_branch, args.probe_ref):
        require(value is None or isinstance(value, str) and 0 < len(value) <= 200, 'invalid_branch')
    installation = installation or Installation(args.ssh)
    installation.host.fingerprint = (installation.journal.read() or {}).get('host_key')
    reply = installation.host.rpc(HOST, dict(repo=args.repo, probe_repo=args.probe_repo, target_branch=args.target_branch,
        probe_ref=args.probe_ref, apply=args.apply, receipt=args.receipt, check_workflow=args.check_workflow), timeout=330)
    report = reply.get('result')
    require(isinstance(report, dict) and report.get('status') in ('planned', 'pending', 'blocked', 'qualified', 'observed'), 'invalid_host_response')
    if report['status'] == 'planned':
        if args.check_workflow:
            report['next_action'] = 'Use --apply to test workflow-file changes in the selected scratch repository. No workflow is dispatched and no permission is granted.'
            return report
        report['next_action'] = ('Use --apply to verify the saved host receipt; no GitHub writes will repeat.' if args.receipt else
            'Use --apply to create a commit and update the selected scratch ref. This does not create repositories, grant permissions, or start Forge.')
    return report

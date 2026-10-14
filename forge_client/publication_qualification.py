"""Supported transport for a host-owned scratch signing qualification."""
import re
import json
import subprocess
from .contracts import NAME, require
from .bootstrap import Host
from .migration import service_user, transport

HOST = '''import json,os,pwd,subprocess,sys
from pathlib import Path
def main():
    value=json.loads(sys.stdin.buffer.read(16385))
    source=Path(value.pop('source'));root=Path(value.pop('root'));user=value.pop('service_user')
    account=pwd.getpwuid(os.geteuid())
    if (account.pw_uid<=0 or account.pw_name!=user or not source.is_absolute() or not root.is_absolute()
            or any(not path.is_dir() or path.resolve()!=path or path.stat().st_uid!=account.pw_uid for path in (source,root))):
        print(json.dumps({'status':'blocked','reason':'existing_installation'}));return 0
    if any(not (source/'forge'/name).is_file() for name in ('publication_qualification.py','launch.py')):
        print(json.dumps({'status':'blocked','reason':'qualification_module_not_installed'}));return 0
    result=subprocess.run(['/usr/bin/python3','-m','forge.launch','forge.publication_qualification','--root',str(root)],
        input=json.dumps(value).encode(),cwd=source,capture_output=True,timeout=300,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':account.pw_dir,'LANG':'C.UTF-8'})
    if result.returncode or len(result.stdout)>65536:
        print(json.dumps({'status':'blocked','reason':'publication_qualification_unconfirmed'}));return 0
    print(json.dumps(json.loads(result.stdout)));return 0
sys.exit(main())
'''


def execute(args, *, run=subprocess.run):
    require(NAME.fullmatch(args.repo or '') and NAME.fullmatch(args.probe_repo or '') and args.repo != args.probe_repo,
            'invalid_repository', 'Choose the connected target and a different existing private scratch repository.')
    require(args.receipt is None or re.fullmatch('[a-f0-9]{32}', args.receipt), 'invalid_receipt')
    for value in (args.target_branch, args.probe_ref):
        require(value is None or isinstance(value, str) and 0 < len(value) <= 200, 'invalid_branch')
    Host(args.ssh, args.source, args.root)
    user = service_user(args)
    require(user is not None, 'invalid_service_user', 'Select the existing service account with --service-user.')
    payload = dict(repo=args.repo, probe_repo=args.probe_repo, target_branch=args.target_branch,
        probe_ref=args.probe_ref, apply=args.apply, receipt=args.receipt, check_workflow=args.check_workflow,
        service_user=user, source=args.source, root=args.root)
    try:
        result = run(transport(args, ['python3', '-I', '-c', HOST]), input=json.dumps(payload).encode(), capture_output=True, timeout=330)
    except (OSError, subprocess.TimeoutExpired):
        require(False, 'qualification_host_unavailable', 'Could not reach qualification on the selected host; check SSH access and installed release.')
    require(result.returncode == 0 and len(result.stdout) <= 65536, 'qualification_host_unavailable',
            'Could not run qualification as the selected service account. Check SSH access and the installed host release.')
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        require(False, 'invalid_host_response')
    require(isinstance(report, dict) and report.get('status') in ('planned', 'pending', 'blocked', 'qualified', 'observed'), 'invalid_host_response')
    if report['status'] == 'planned':
        if args.check_workflow:
            report['next_action'] = 'Use --apply to test workflow-file changes in the selected scratch repository. No workflow is dispatched and no permission is granted.'
            return report
        report['next_action'] = ('Use --apply to verify the saved host receipt; no GitHub writes will repeat.' if args.receipt else
            'Use --apply to create a commit and update the selected scratch ref. This does not create repositories, grant permissions, or start Forge.')
    return report

"""Bounded role acceptance on an installed, idle host."""
import math
import re
from .contracts import SetupError, require
from .install import Installation

ROLES = ('build','review','audit','recovery','triage','strategist','discussion','steward')

HOST = '''import json,pwd,re,subprocess,sys
from pathlib import Path
def main(payload):
    value=json.loads(sys.stdin.buffer.read(4097))
    if (set(value)!={'version','repo','issue','apply','roles'} or value['version']!=1 or type(value['apply']) is not bool
        or type(value['issue']) is not int or not 1<=value['issue']<=2147483647
        or not isinstance(value['repo'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value['repo'])
        or not isinstance(value['roles'],list) or not value['roles']
        or any(role not in ('build','review','audit','recovery','triage','strategist','discussion','steward') for role in value['roles'])
        or len(set(value['roles']))!=len(value['roles'])):
        print(json.dumps({'ok':False,'error':'invalid_request'}));return 1
    account=pwd.getpwnam('forge');source=Path('/home/forge/forge')
    if account.pw_uid<=0 or account.pw_dir!='/home/forge' or source.is_symlink() or source.stat().st_uid!=account.pw_uid:
        print(json.dumps({'ok':False,'error':'existing_installation'}));return 1
    environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}
    args=['runuser','-u','forge','--','env','-i',*['%s=%s'%(k,v) for k,v in environment.items()],
        'HOME=/home/forge','/usr/bin/python3','-m','forge.launch','forge.role_qualification',
        '--root','/home/forge/.local/share/forge','--repo',value['repo'],'--issue',str(value['issue'])]
    for role in value['roles']:args.extend(['--role',role])
    if value['apply']:args.append('--apply')
    result=subprocess.run(args,cwd=source,capture_output=True,timeout=1800 if value['apply'] else 60,env=environment)
    if result.returncode not in (0,1) or len(result.stdout)>65536:
        print(json.dumps({'ok':False,'error':'role_qualification_failed'}));return 1
    try:report=json.loads(result.stdout)
    except ValueError:
        print(json.dumps({'ok':False,'error':'role_qualification_failed'}));return 1
    print(json.dumps({'ok':True,'result':report}));return 0
'''


class RoleQualificationFailed(SetupError):
    """A failed acceptance run with credential-free host observations."""
    def __init__(self, report):
        super().__init__('role_qualification_failed', 'Role qualification did not complete; inspect the report before retrying.')
        self.report = failure_report(report)



def provider_session(value):
    """Credential-free native metadata; host runtime is absent in CLI installs."""
    if (not isinstance(value, dict) or value.get('source') != 'native_session'
            or value.get('usage_scope') != 'last_recorded' or type(value.get('truncated')) is not bool):
        return None
    result = dict(source='native_session', usage_scope='last_recorded', truncated=value['truncated'])
    models = {'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra',
              'gpt-5.6-luna', 'gpt-daybreak-blue-latest', 'gpt-daybreak-red-latest', 'gpt-5.5',
              'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.2', 'codex-auto-review', 'unrecognized'}
    efforts = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'unrecognized'}
    for field, allowed in (('model', models), ('reasoning_effort', efforts)):
        if isinstance(value.get(field), str) and value[field] in allowed:
            result[field] = value[field]
    numeric = lambda item: type(item) is int and 0 <= item <= 10**12
    fields = {'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
              'output_tokens', 'reasoning_output_tokens', 'total_tokens'}
    for field in ('last_token_usage', 'total_token_usage'):
        if isinstance(value.get(field), dict):
            result[field] = {key: value[field][key] for key in fields if key in value[field] and numeric(value[field][key])}
    if numeric(value.get('model_context_window')):
        result['model_context_window'] = value['model_context_window']
    return result


def provider_diagnostic(value):
    """Project a fixed vocabulary without importing the host runtime package."""
    if (not isinstance(value, dict) or value.get('kind') != 'unclassified_terminal_error'
            or value.get('event_scope') != 'retained_tail'):
        return None
    events = {'thread.started', 'turn.started', 'error', 'command_execution', 'agent_message',
              'reasoning', 'file_change', 'mcp_tool_call'}
    fields = {'message', 'code', 'type', 'status', 'param'}
    types = {'null', 'boolean', 'number', 'string', 'array', 'object'}
    codes = {'invalid_json_schema', 'invalid_request_error', 'context_length_exceeded', 'invalid_api_key',
             'rate_limit_exceeded', 'insufficient_quota', 'model_not_found', 'server_error', 'authentication_error'}
    observed, error_fields = value.get('observed'), value.get('error_fields')
    if (not isinstance(observed, list) or not isinstance(error_fields, dict)
            or type(value.get('extra_error_fields')) is not bool):
        return None
    result = dict(kind='unclassified_terminal_error', event_scope='retained_tail',
                  observed=sorted({item for item in observed[:8] if isinstance(item, str) and item in events}),
                  error_fields={key: error_fields[key] for key in fields if isinstance(error_fields.get(key), str)
                                and error_fields[key] in types}, extra_error_fields=value['extra_error_fields'])
    code, status = value.get('structured_code'), value.get('http_status')
    if isinstance(code, str) and code in codes:
        result['structured_code'] = code
    if type(status) is int and 400 <= status <= 599:
        result['http_status'] = status
    session = provider_session(value.get('session'))
    if session is not None:
        result['session'] = session
    return result


def failure_report(value):
    """Keep bounded execution identities and outcomes, never capture contents."""
    roles = set(ROLES)
    enums = {'status': {'failed', 'qualified', 'planned'}, 'current_role': roles,
             'cleanup': {'confirmed', 'unconfirmed', 'pending'},
             'automation': {'disabled'}, 'automation_after': {'disabled', 'changed'},
             'diagnostic_status': {'unavailable'}, 'role': roles, 'provider': {'codex', 'claude', 'grok'},
             'outcome': {'pass', 'reject', 'unable', 'wait', 'blocked_environment'}}
    patterns = {'diagnostic_id': r'[0-9a-f]{32}', 'source_revision': r'[0-9a-f]{40}', 'image': r'sha256:[0-9a-f]{64}',
                'source_digest': r'[0-9a-f]{64}', 'base_oid': r'(?:[0-9a-f]{40}|[0-9a-f]{64})',
                'job_id': r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                'reason': r'[a-z][a-z_]{0,79}'}
    def project(row, keys):
        result = {}
        for key in keys:
            item = row.get(key)
            if key in enums and isinstance(item, str) and item in enums[key]:
                result[key] = item
            elif key in patterns and isinstance(item, str) and re.fullmatch(patterns[key], item):
                result[key] = item
            elif key == 'fixture' and type(item) is bool:
                result[key] = item
            elif key == 'mirror_requests' and type(item) is int and 0 <= item <= 1000000:
                result[key] = item
        return result
    report = project(value, ('status', 'reason', 'current_role', 'cleanup', 'source_revision', 'image',
                             'automation', 'automation_after', 'diagnostic_id', 'diagnostic_status'))
    rows = value.get('results')
    if isinstance(rows, list):
        report['results'] = [project(row, ('role', 'status', 'job_id', 'provider', 'base_oid', 'source_digest',
                                         'fixture', 'outcome', 'mirror_requests'))
                             for row in rows[:len(ROLES)] if isinstance(row, dict)]
    failure = value.get('failure')
    if isinstance(failure, dict):
        diagnostic = project(failure, ('role', 'job_id'))
        if type(failure.get('result_present')) is bool:
            diagnostic['result_present'] = failure['result_present']
        missing = failure.get('missing')
        if isinstance(missing, list):
            codes = {'result', 'logs', 'bundle', 'output_receipt', 'startup_receipt', 'execution_envelope', 'container'}
            diagnostic['missing'] = sorted({item for item in missing[:7] if isinstance(item, str) and item in codes})
        execution = failure.get('execution')
        if isinstance(execution, dict):
            diagnostic['execution'] = {key: execution[key] for key in ('running', 'oom_killed')
                                       if type(execution.get(key)) is bool}
            code = execution.get('exit_code')
            if type(code) is int and -255 <= code <= 255:
                diagnostic['execution']['exit_code'] = code
        envelope = failure.get('execution_envelope')
        if isinstance(envelope, dict) and envelope.get('harness_status') in ('ok', 'timeout', 'harness_error', 'provider_error'):
            safe = {'harness_status': envelope['harness_status']}
            provider = envelope.get('provider_error')
            if isinstance(provider, dict) and provider.get('code') in ('quota', 'auth', 'rate_limit', 'overloaded', 'unknown'):
                safe['provider_error'] = {'code': provider['code']}
                status, retry = provider.get('status'), provider.get('retry_after')
                if type(status) is int and 400 <= status <= 599:
                    safe['provider_error']['status'] = status
                if type(retry) in (int, float) and math.isfinite(retry) and 1 <= retry <= 3600:
                    safe['provider_error']['retry_after'] = retry
                provider_detail = provider_diagnostic(provider.get('diagnostic'))
                if provider_detail is not None:
                    safe['provider_error']['diagnostic'] = provider_detail
            harness = envelope.get('harness_error')
            if isinstance(harness, dict) and harness.get('phase') in ('harness', 'runner', 'before_agent', 'contract'):
                safe['harness_error'] = {'phase': harness['phase']}
            diagnostic['execution_envelope'] = safe
        startup = failure.get('startup')
        if (isinstance(startup, dict) and startup.get('reason') == 'harness_startup'
                and startup.get('phase') == 'before_agent'
                and startup.get('exception') in ('ImportError', 'ModuleNotFoundError', 'NameError',
                    'UnboundLocalError', 'SyntaxError', 'IndentationError', 'TabError')):
            diagnostic['startup'] = {key: startup[key] for key in ('reason', 'phase', 'exception')}
        report['failure'] = diagnostic
    return report


def execute(args, *, installation=None):
    require(isinstance(args.repo,str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',args.repo)
            and type(args.issue) is int and 1<=args.issue<=2147483647,'invalid_request')
    roles=getattr(args,'role',None)
    roles=list(ROLES) if roles is None else roles
    require(isinstance(roles,list) and bool(roles) and all(role in ROLES for role in roles)
            and len(set(roles))==len(roles),'invalid_request')
    installation=installation or Installation(args.ssh)
    installation.host.fingerprint=(installation.journal.read() or {}).get('host_key')
    response=installation.host.rpc(HOST,dict(version=1,repo=args.repo,issue=args.issue,apply=args.apply,roles=roles),
                                   timeout=1830 if args.apply else 90)
    report=response.get('result')
    require(isinstance(report,dict),'invalid_host_response')
    if report.get('status') != ('qualified' if args.apply else 'planned'):
        raise RoleQualificationFailed(report)
    require(report.get('roles')==roles,'invalid_host_response')
    if args.apply:
        if report.get('cleanup') != 'confirmed' or report.get('automation_after') != 'disabled':
            raise RoleQualificationFailed(report)
    else:
        report['next_action']=f'Use --apply to run {len(roles)} bounded provider invocations. The factory must remain disabled and idle.'
    return report

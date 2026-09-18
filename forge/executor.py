"""Protected SYSTEM-service executor scaffold; real CLI activation is disabled.

Plans accept identities only, never task-supplied commands/properties/paths.
The only activatable workloads are fixed, credential-free synthetic fixtures.
Root provisioning is separate. RuntimeMaxSec bounds controller disappearance;
controller SIGKILL is not immediate cancellation. Shared provider UIDs are NOT
cross-attempt process/credential isolation; production remains gated on that proof.
"""
from dataclasses import dataclass
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import uuid

ROOT = Path('/var/lib/forge-executor/attempts')
CGROUP_ROOT = Path('/sys/fs/cgroup')
PROVIDERS = {'codex', 'claude', 'grok'}
POLICY_TARGETS = {'codex': '/etc/codex', 'claude': '/etc/claude-code', 'grok': '/etc/grok'}
POLICY_FILES = {'codex': {'requirements.toml', 'config.toml'},
                'claude': {'managed-settings.json', 'managed-mcp.json'},
                'grok': {'requirements.toml', 'managed_config.toml'}}
SHOW_FIELDS = ('Id', 'LoadState', 'ActiveState', 'SubState', 'ControlGroup', 'InvocationID',
               'User', 'Group', 'ExecMainCode', 'ExecMainStatus')


class ExecutorError(RuntimeError):
    pass


@dataclass(frozen=True)
class AttemptPlan:
    provider: str
    attempt_id: str

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError('Unknown provider')
        try:
            parsed = uuid.UUID(self.attempt_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError('Canonical UUID4 attempt identity required') from error
        if str(parsed) != self.attempt_id or parsed.version != 4:
            raise ValueError('Canonical UUID4 attempt identity required')

    @classmethod
    def fresh(cls, provider):
        return cls(provider, str(uuid.uuid4()))

    @property
    def unit(self):
        return f'forge-exec-{self.provider}-{uuid.UUID(self.attempt_id).hex}.service'

    @property
    def user(self):
        return 'forge-' + self.provider

    @property
    def directory(self):
        return ROOT / self.attempt_id

    @property
    def cgroup(self):
        return '/system.slice/' + self.unit


@dataclass(frozen=True)
class Receipt:
    provider: str
    attempt_id: str
    unit: str
    invocation_id: str
    cgroup: str

    def validate(self, plan):
        if ((self.provider, self.attempt_id, self.unit, self.cgroup) !=
                (plan.provider, plan.attempt_id, plan.unit, plan.cgroup) or
                not re.fullmatch(r'[0-9a-f]{32}', self.invocation_id)):
            raise ExecutorError('Receipt does not identify this exact attempt')


def _fixed_command(plan, scenario):
    if scenario == 'cli-plan':
        # Inspection only. Real activation never calls this command.
        return ['/opt/forge/bin/' + plan.provider, '--version']
    if scenario not in {'probe', 'linger'}:
        raise ValueError('Only fixed synthetic scenarios may execute')
    return ['/usr/bin/python3', '-I', '/opt/forge-executor/fixture.py', scenario, plan.provider]



def command_plan(plan, scenario='cli-plan'):
    """Pure dry-run argv. No arbitrary command/property/credential parameters."""
    command = _fixed_command(plan, scenario)
    environment = ['HOME=/home/forge/client', 'USER=' + plan.user, 'LOGNAME=' + plan.user,
                   'PATH=/usr/bin:/bin', 'LANG=C.UTF-8']
    if plan.provider == 'codex':
        environment += ['CODEX_HOME=/home/forge/client']
    elif plan.provider == 'grok':
        environment += ['GROK_HOME=/home/forge/client', 'GROK_DISABLE_AUTOUPDATER=1',
                        'GROK_TELEMETRY_ENABLED=0', 'GROK_FEEDBACK_ENABLED=0']
    else:
        environment += ['CLAUDE_CONFIG_DIR=/home/forge/client', 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1',
                        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1', 'DISABLE_AUTOUPDATER=1']
    command = ['/usr/bin/env', '-i', *environment, *command]
    directory = plan.directory
    properties = {
        'Description': 'Forge synthetic executor ' + plan.attempt_id,
        'Slice': 'system.slice', 'User': plan.user, 'Group': plan.user,
        'SupplementaryGroups': '', 'UMask': '0077', 'Restart': 'no',
        'KillMode': 'control-group', 'KillSignal': 'SIGTERM', 'SendSIGKILL': 'yes',
        'TimeoutStopSec': '3s', 'RuntimeMaxSec': '20s', 'TasksMax': '32',
        'MemoryMax': '512M', 'MemorySwapMax': '0', 'OOMPolicy': 'kill',
        'NoNewPrivileges': 'yes', 'CapabilityBoundingSet': '', 'AmbientCapabilities': '',
        'PrivateDevices': 'yes', 'PrivateTmp': 'yes', 'PrivateNetwork': 'yes',
        'PrivateMounts': 'yes', 'ProtectSystem': 'strict', 'ProtectHome': 'tmpfs',
        'ProtectKernelTunables': 'yes', 'ProtectKernelModules': 'yes', 'ProtectKernelLogs': 'yes',
        'ProtectControlGroups': 'yes', 'RestrictSUIDSGID': 'yes', 'LockPersonality': 'yes',
        'ProtectProc': 'invisible', 'ProcSubset': 'pid',
        # Hide *all* host runtime sockets and host state, including other attempts.
        'TemporaryFileSystem': '/run:ro /var/lib:ro /etc:ro',
        'InaccessiblePaths': '-/etc/forge -/etc/ssh',
        'BindPaths': f'{directory}/workspace:/home/forge/workspace {directory}/client:/home/forge/client',
        # Bind the entire managed directory: sibling managed settings cannot bleed in.
        'BindReadOnlyPaths': (f'{directory}/policy/{plan.provider}:{POLICY_TARGETS[plan.provider]} ' +
                              (f'{directory}/policy/codex/config.toml:/home/forge/client/config.toml ' if plan.provider == 'codex' else '') +
                              '/etc/passwd:/etc/passwd /etc/group:/etc/group -/etc/ld.so.cache:/etc/ld.so.cache'),
        'ReadWritePaths': '/home/forge/workspace /home/forge/client',
        'WorkingDirectory': '/home/forge/workspace',
        'Environment': 'HOME=/home/forge/client PATH=/usr/bin:/bin LANG=C.UTF-8',
        'PassEnvironment': '',
        'UnsetEnvironment': ('SSH_AUTH_SOCK SSH_AGENT_PID DBUS_SESSION_BUS_ADDRESS DBUS_SYSTEM_BUS_ADDRESS '
                             'XDG_RUNTIME_DIR NOTIFY_SOCKET LISTEN_FDS LISTEN_PID LISTEN_FDNAMES '
                             'ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN '
                             'OPENAI_API_KEY CODEX_API_KEY XAI_API_KEY GROK_DEPLOYMENT_KEY '
                             'GROK_AUTH_PROVIDER_COMMAND GROK_CONFIG GROK_CONFIG_PATH '
                             'GH_TOKEN GITHUB_TOKEN PYTHONPATH PYTHONHOME '
                             'LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES'),
        'StandardInput': 'null', 'StandardOutput': 'journal', 'StandardError': 'journal',
    }
    args = ['/usr/bin/systemd-run', '--system', '--quiet', '--service-type=exec', '--unit=' + plan.unit]
    args += ['--property=' + name + '=' + value for name, value in properties.items()]
    return args + ['--', *command]


def _check_path(path, *, owner=0, directory=False):
    path = Path(path)
    for ancestor in reversed(path.parents):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ExecutorError('Untrusted ancestry: ' + str(ancestor))
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ExecutorError('Unexpected ownership/type/mode: ' + str(path))
    if not directory and info.st_nlink != 1:
        raise ExecutorError('Hard-linked policy/executable is not accepted')
    return info


def verify_preconditions(plan):
    """Read-only preflight; never creates accounts, policy files or workspaces."""
    account = pwd.getpwnam(plan.user)
    if (type(account.pw_uid) is not int or type(account.pw_gid) is not int or
            account.pw_uid <= 0 or account.pw_gid <= 0 or
            grp.getgrnam(plan.user).gr_gid != account.pw_gid or
            set(os.getgrouplist(plan.user, account.pw_gid)) != {account.pw_gid}):
        raise ExecutorError('Provider identity has elevated or supplementary groups')
    _check_path('/opt/forge-executor/fixture.py')
    _check_path(plan.directory, directory=True)
    for name in ('workspace', 'client'):
        info = _check_path(plan.directory / name, owner=account.pw_uid, directory=True)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_gid != account.pw_gid:
            raise ExecutorError('Private provider-owned workspace/client directory required')
    policy = plan.directory / 'policy' / plan.provider
    _check_path(policy, directory=True)
    if {p.name for p in policy.iterdir()} != POLICY_FILES[plan.provider]:
        raise ExecutorError('Unexpected or missing managed policy files')
    for name in POLICY_FILES[plan.provider]:
        _check_path(policy / name)
    binary = Path('/opt/forge/bin') / plan.provider
    info = _check_path(binary)
    if stat.S_IMODE(info.st_mode) != 0o750 or info.st_gid != account.pw_gid:
        raise ExecutorError('Pinned CLI must be root:provider mode 0750')
    manifest = Path('/etc/forge/isolation/manifest.json')
    _check_path(manifest)
    clients = json.loads(manifest.read_text())['clients']
    entries = [c for c in clients if c['provider'] == plan.provider and c['target'] == str(binary)]
    with binary.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    if len(entries) != 1 or entries[0]['sha256'] != checksum:
        raise ExecutorError('CLI bytes do not match root-owned provisioning manifest')


class SystemdExecutor:
    """Lifecycle keyed by service invocation and exact cgroup, never process IDs.

    Trusted controller must persist Receipt before admitting replacement work.
    Missing/corrupt receipts require reconciliation, not a guessed clean result.
    """
    def __init__(self, run=subprocess.run, read_text=None, exists=None):
        self.run = run
        self.read_text = read_text or (lambda path: Path(path).read_text())
        self.exists = exists or (lambda path: Path(path).exists())

    def _show(self, plan):
        result = self.run(['/usr/bin/systemctl', '--system', 'show', plan.unit,
                           '--property=' + ','.join(SHOW_FIELDS)],
                          capture_output=True, text=True, timeout=10)
        # Even not-found units have a structured show response. Never parse a
        # command error as absence or use stderr substrings to permit replacement.
        values = {}
        for line in result.stdout.splitlines():
            name, separator, value = line.partition('=')
            if not separator or name in values or name not in SHOW_FIELDS:
                raise ExecutorError('Malformed service-manager observation')
            values[name] = value
        if set(values) != set(SHOW_FIELDS) or values['Id'] != plan.unit:
            raise ExecutorError('Service manager did not identify the exact unit')
        if result.returncode and not (result.returncode == 1 and values['LoadState'] == 'not-found'):
            raise ExecutorError('Service-manager read failed')
        return values

    def _identity(self, plan, values, receipt=None):
        if values['LoadState'] == 'not-found':
            if (values['ActiveState'] != 'inactive' or values['ControlGroup'] or
                    values['InvocationID'] or values['User'] or values['Group']):
                raise ExecutorError('Inconsistent absent-unit observation')
            return
        if (values['LoadState'] != 'loaded' or values['User'] != plan.user or values['Group'] != plan.user or
                values['ControlGroup'] not in {'', plan.cgroup} or
                not re.fullmatch(r'[0-9a-f]{32}', values['InvocationID'])):
            raise ExecutorError('Unit identity/cgroup does not match protected attempt')
        if receipt and values['InvocationID'] != receipt.invocation_id:
            raise ExecutorError('Unit was replaced by another invocation')

    def start(self, plan, *, apply=False):
        if apply:
            raise ExecutorError('Real CLI activation is disabled pending isolation and credential gates')
        return {'dry_run': True, 'unit': plan.unit, 'argv': command_plan(plan)}

    def start_synthetic(self, plan, *, scenario='linger', apply=False):
        argv = command_plan(plan, scenario)
        if scenario not in {'probe', 'linger'}:
            raise ExecutorError('Only synthetic fixtures can activate')
        if not apply:
            return {'dry_run': True, 'unit': plan.unit, 'argv': argv}
        if os.geteuid() != 0:
            raise ExecutorError('Explicit root execution required; no implicit sudo escalation')
        verify_preconditions(plan)
        before = self._show(plan)
        if before['LoadState'] != 'not-found' or self.exists(CGROUP_ROOT / plan.cgroup.lstrip('/')):
            raise ExecutorError('Attempt unit/cgroup already exists; never reuse an identity')
        result = self.run(argv, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ExecutorError('Start outcome uncertain; reconcile exact unit before any replacement')
        after = self._show(plan)
        self._identity(plan, after)
        if after['LoadState'] != 'loaded' or after['ControlGroup'] != plan.cgroup:
            raise ExecutorError('Start identity not confirmed; reconcile before replacement')
        return Receipt(plan.provider, plan.attempt_id, plan.unit, after['InvocationID'], plan.cgroup)

    def reconcile(self, plan, receipt):
        receipt.validate(plan)
        values = self._show(plan)
        self._identity(plan, values, receipt)
        cgroup = CGROUP_ROOT / receipt.cgroup.lstrip('/')
        if self.exists(cgroup):
            rows = {}
            for line in self.read_text(cgroup / 'cgroup.events').splitlines():
                key, value = line.split()
                if key in rows:
                    raise ExecutorError('Malformed cgroup observation')
                rows[key] = value
            if rows.get('populated') not in {'0', '1'}:
                raise ExecutorError('Unknown cgroup population')
            empty = rows['populated'] == '0'
        else:
            empty = True
        stopped = values['LoadState'] == 'not-found' or values['ActiveState'] in {'inactive', 'failed'}
        return {'unit': plan.unit, 'invocation_id': receipt.invocation_id,
                'state': values['ActiveState'], 'cgroup_empty': empty,
                'cleanup_verified': stopped and empty,
                'exit_status': values['ExecMainStatus'] if values['LoadState'] != 'not-found' else None}

    def cancel(self, plan, receipt, *, apply=False):
        receipt.validate(plan)
        argv = ['/usr/bin/systemctl', '--system', 'stop', plan.unit]
        if not apply:
            return {'dry_run': True, 'unit': plan.unit, 'argv': argv}
        if os.geteuid() != 0:
            raise ExecutorError('Explicit root cancellation required')
        values = self._show(plan)
        self._identity(plan, values, receipt)
        if values['LoadState'] != 'not-found':
            result = self.run(argv, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ExecutorError('Cancellation unconfirmed; no fallback process-group kill')
        result = self.reconcile(plan, receipt)
        if not result['cleanup_verified']:
            raise ExecutorError('Cgroup cleanup remains unverified; replacement forbidden')
        return result

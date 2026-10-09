"""CLI-owned maintenance window for a user-systemd legacy Forge installation."""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

from .snapshot import MigrationError, capture, sources, decode, encode, digest, read_regular, operation_lock, uncleaned_auxiliary
from ...state import atomic_json
from ..db import _fsync_directory, qualified_sqlite
from ...factory.ledger import Ledger


class Host:
    def __init__(self, root, source, *, run=subprocess.run):
        self.root,self.source=Path(root).absolute(),Path(source).absolute()
        self.run=run
        self.journal=self.root/'.migration-maintenance.json'

    def command(self,*args):
        result=self.run(list(args),capture_output=True,text=True,timeout=60)
        if result.returncode:
            raise MigrationError('host_command_failed')
        return result.stdout.strip()

    def units(self):
        raw=self.command('systemctl','--user','list-units','--all','--output=json','--no-pager')
        rows=json.loads(raw)
        return sorted(row['unit'] for row in rows if re.fullmatch(r'forge[-a-z0-9]*\.(service|timer)',row['unit'])
                      and row['active'] in ('active','activating','reloading'))

    def identity(self):
        command=self.command('systemctl','--user','show','forge-supervisor.service','-p','ExecStart','--value')
        if str(self.root) not in command:
            raise MigrationError('service_root_mismatch')
        return self.command('git','-C',str(self.source),'rev-parse','HEAD')

    def inspect(self):
        revision=self.identity()
        data=sources(self.root)
        ledger=decode(data['state/factory/factory-ledger.json'])
        Ledger.validate(ledger)
        auxiliary=uncleaned_auxiliary(data)
        blockers=[]
        try:
            cron=self.run(['crontab','-l'],capture_output=True,text=True,timeout=10)
            if cron.returncode not in (0,1):
                raise MigrationError('schedule_inspection_failed')
            if any(word in cron.stdout for word in (str(self.root),str(self.source),'forge')):
                blockers.append('unmanaged_schedule')
        except FileNotFoundError:
            pass
        if ledger['slots'] or any(not j['cleanup'] for j in ledger['jobs'].values()) or auxiliary:
            blockers.append('uncleaned_jobs')
        revision_hash=digest(encode({'source_revision':revision,'units':self.units(),
                                    'root':str(self.root),'sources':{n:digest(raw) for n,raw in sorted(data.items())}}))
        from .runtime import inspect as runtime_inspect
        prepared=runtime_inspect(self.root)
        return dict(status='blocked' if blockers else 'planned',plan=revision_hash,source_revision=revision,
                    source_files=len(data),source_bytes=sum(len(v) for v in data.values()),
                    services=self.units(),blockers=blockers,
                    sqlite_version=sqlite3.sqlite_version,sqlite_qualified=qualified_sqlite(),
                    prepared_sqlite=prepared,
                    free_bytes=shutil.disk_usage(self.root).free,
                    cutover_blockers=[] if qualified_sqlite() or prepared else ['sqlite_unqualified'],
                    effects=['Stop Forge API/UI and background writers briefly',
                    'Capture and hash a private diagnostic snapshot','Restore previously active services'],activation=False)

    def quiet(self):
        if self.units():
            raise MigrationError('writers_not_stopped')
        # Catch manual controller launches outside managed service cgroups.
        ancestors={os.getpid()}; pid=os.getppid()
        while pid>1 and pid not in ancestors:
            ancestors.add(pid)
            try:
                pid=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[1])
            except (OSError,ValueError):
                break
        for process in Path('/proc').iterdir():
            if not process.name.isdigit() or int(process.name) in ancestors:
                continue
            try:
                if process.stat().st_uid!=os.getuid():
                    continue
                command=(process/'cmdline').read_bytes()
            except (FileNotFoundError,PermissionError,ProcessLookupError):
                continue
            if (str(self.root).encode() in command or str(self.source).encode() in command) and b'git\x00' not in command:
                raise MigrationError('unmanaged_process')

    def prepare_source_update(self, previous, target, tool_version):
        """Read/fetch and qualify a pinned fast-forward without running host setup."""
        import io
        import tarfile
        if not re.fullmatch(r'[0-9a-f]{40}', target):
            raise MigrationError('repair_revision_required')
        current=self.identity()
        if current not in {previous,target}:
            raise MigrationError('repair_source_changed')
        if self.command('git','-C',str(self.source),'status','--porcelain','--untracked-files=no'):
            raise MigrationError('repair_source_not_clean')
        self.command('git','-C',str(self.source),'fetch','--no-tags','origin',target)
        if self.command('git','-C',str(self.source),'rev-parse',target+'^{commit}')!=target:
            raise MigrationError('repair_revision_mismatch')
        ancestry=self.run(['git','-C',str(self.source),'merge-base','--is-ancestor',previous,target],capture_output=True,timeout=30)
        if ancestry.returncode:
            raise MigrationError('repair_requires_forward_revision')
        archive=self.run(['git','-C',str(self.source),'archive','--format=tar',target,'forge'],capture_output=True,timeout=60)
        if archive.returncode or len(archive.stdout)>32*1024*1024:
            raise MigrationError('repair_source_unavailable')
        records={}
        with tarfile.open(fileobj=io.BytesIO(archive.stdout),mode='r:') as contents:
            for member in contents:
                if member.isdir() and member.name.rstrip('/')=='forge':continue
                path=Path(member.name)
                if not member.name.startswith('forge/') or '..' in path.parts:
                    raise MigrationError('repair_source_unsafe')
                if not (path.suffix in {'.py','.sql'} or path.match('forge/harnesses/*/manifest.json')):
                    continue
                if not member.isfile() or member.size>4*1024*1024 or member.name in records:
                    raise MigrationError('repair_source_unsafe')
                records[member.name.removeprefix('forge/')]=contents.extractfile(member).read()
        fingerprint='1:'+digest(b''.join(name.encode()+records[name] for name in sorted(records,key=Path)))
        if not records or fingerprint!=tool_version:
            raise MigrationError('repair_bundle_source_mismatch')

    def install_source_update(self, previous, target):
        """Only advance the checkout; service/runtime reconciliation is forbidden."""
        current=self.identity()
        if current not in {previous,target}:
            raise MigrationError('repair_source_changed')
        if self.command('git','-C',str(self.source),'status','--porcelain','--untracked-files=no'):
            raise MigrationError('repair_source_not_clean')
        if current!=target:
            self.command('git','-c','core.hooksPath=/dev/null','-C',str(self.source),'merge','--ff-only',target)
        if self.identity()!=target:
            raise MigrationError('repair_revision_mismatch')

    def restore(self, *, accept_tool_update=False, source_revision=None):
        if (self.root/'.migration-cutover.json').exists():
            from .cutover import Cutover
            return Cutover(self).resume(accept_tool_update=accept_tool_update, source_revision=source_revision)
        if accept_tool_update or source_revision:
            raise MigrationError('cutover_not_started')
        with operation_lock(self.root, '.migration-host.lock'):
            return self._restore()

    def _restore(self):
        if not self.journal.exists():
            return {'status':'restored','services':[]}
        value=decode(read_regular(self.journal,limit=65536))
        units=value['services']
        if any(not re.fullmatch(r'forge[-a-z0-9]*\.(service|timer)',v) for v in units):
            raise MigrationError('invalid_maintenance_journal')
        if units:
            self.command('systemctl','--user','start',*units)
        if not set(units).issubset(set(self.units())):
            raise MigrationError('service_restore_incomplete')
        self.journal.unlink()
        _fsync_directory(self.root)
        return {'status':'restored','services':units}

    def capture(self,destination,expected_plan):
        with operation_lock(self.root, '.migration-host.lock'):
            return self._capture(destination,expected_plan)

    def _capture(self,destination,expected_plan):
        if self.journal.exists():
            raise MigrationError('maintenance_resume_required')
        plan=self.inspect()
        if plan['blockers']:
            raise MigrationError('uncleaned_jobs')
        if plan['plan']!=expected_plan:
            raise MigrationError('plan_changed')
        # Journal before stopping anything. A later CLI resume restores services
        # after an SSH loss/process kill; no database can be activated here.
        atomic_json(self.journal,{'services':plan['services'],'at':time.time()})
        try:
            if plan['services']:
                self.command('systemctl','--user','stop',*plan['services'])
            self.quiet()
            snapshot=capture(self.root,destination,source_revision=plan['source_revision'],storage_generation=1,quiescent=self.quiet)
            return {'status':'captured','manifest':snapshot.id,'source_files':len(snapshot.entries),'activation':False}
        finally:
            self._restore()


def failure(error, request):
    """Safe transport diagnostics: never include exception text or source rows."""
    from ..db import StoreError
    stage='before_journal'
    path=Path(request['root'])/'.migration-cutover.json'
    if path.exists():
        try:
            value=decode(read_regular(path,limit=1048576))
            candidate=value.get('stage')
            stage=candidate if candidate in {'draining','stopping','capturing','importing','validated','selected','complete'} else 'journal_unreadable'
        except Exception:
            stage='journal_unreadable'
    name=type(error).__name__
    return dict(ok=False,error=error.code if isinstance(error,(MigrationError,StoreError)) else 'unexpected_migration_failure',
                exception_type=name if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',name) else 'Exception',stage=stage)


def run(request):
    host=Host(request['root'],request['source'])
    if request['action']=='plan':
        if (host.root/'.migration-cutover.json').exists():
            from .cutover import Cutover
            return Cutover(host).status()
        return host.inspect()
    if request['action']=='prepare':
        host.identity()  # Bind the explicit runtime root to the managed host.
        from .runtime import prepare
        return prepare(host.root)
    if request['action']=='resume':
        return host.restore(accept_tool_update=request.get('accept_tool_update') is True, source_revision=request.get('source_revision'))
    if request['action']=='activate':
        if request.get('apply') is not True: raise MigrationError('activation_not_authorized')
        from .cutover import Cutover
        return Cutover(host).activate(expected_plan=request.get('plan'))
    if request['action']=='capture':
        return host.capture(request['destination'],request['plan'])
    raise MigrationError('unknown_migration_action')

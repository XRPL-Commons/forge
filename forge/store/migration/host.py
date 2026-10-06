"""CLI-owned maintenance window for a user-systemd legacy Forge installation."""
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .snapshot import MigrationError, capture, sources, decode, encode, digest, read_regular, operation_lock
from ...state import atomic_json
from ..db import _fsync_directory
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
        auxiliary=0
        for name in ('state/deployments.json','state/issues/discussions.json'):
            def count(value):
                if not isinstance(value,dict):
                    return 0
                if isinstance(value.get('job'),dict) and not value['job'].get('cleanup',False):
                    return 1
                return sum(count(v) for v in value.values() if isinstance(v,dict))
            auxiliary+=count(decode(data[name])) if name in data else 0
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
        return dict(status='blocked' if blockers else 'planned',plan=revision_hash,source_revision=revision,
                    source_files=len(data),source_bytes=sum(len(v) for v in data.values()),
                    services=self.units(),blockers=blockers,effects=['Stop Forge API/UI and background writers briefly',
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

    def restore(self):
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


def run(request):
    host=Host(request['root'],request['source'])
    if request['action']=='plan':
        return host.inspect()
    if request['action']=='resume':
        return host.restore()
    if request['action']=='capture':
        return host.capture(request['destination'],request['plan'])
    raise MigrationError('unknown_migration_action')

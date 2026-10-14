"""CLI-owned maintenance window for a user-systemd Forge installation."""
import json
import os
from pathlib import Path
import re
import subprocess

from .snapshot import MigrationError, decode, digest, read_regular


class Host:
    def __init__(self, root, source, *, run=subprocess.run):
        self.root,self.source=Path(root).absolute(),Path(source).absolute()
        self.run=run

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

    def prepare_source_update(self, previous, target, tool_version, *, source_pack=None):
        """Qualify pinned, forward-only source before any host change."""
        import io
        import tarfile
        if not re.fullmatch(r'[0-9a-f]{40}', target):
            raise MigrationError('repair_revision_required')
        current=self.identity()
        if current not in {previous,target}:
            raise MigrationError('repair_source_changed')
        if self.command('git','-C',str(self.source),'status','--porcelain','--untracked-files=no'):
            raise MigrationError('repair_source_not_clean')
        if source_pack is None:
            present = self.run(['git','-C',str(self.source),'rev-parse','--verify',target+'^{commit}'],
                               capture_output=True,text=True,timeout=30)
            if present.returncode or present.stdout.strip() != target:
                raise MigrationError('source_pack_required')
        else:
            import base64
            try:
                raw=base64.b64decode(source_pack['data'],validate=True)
            except (KeyError,TypeError,ValueError):
                raise MigrationError('source_pack_invalid') from None
            if len(raw)>32*1024*1024 or not raw.startswith(b'PACK') or digest(raw)!=source_pack.get('sha256'):
                raise MigrationError('source_pack_invalid')
            indexed=self.run(['git','-C',str(self.source),'index-pack','--stdin'],input=raw,capture_output=True,timeout=120)
            if indexed.returncode:
                raise MigrationError('source_pack_invalid')
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
        """Only change the checkout; service/runtime reconciliation is forbidden."""
        current=self.identity()
        if current not in {previous,target}:
            raise MigrationError('repair_source_changed')
        if self.command('git','-C',str(self.source),'status','--porcelain','--untracked-files=no'):
            raise MigrationError('repair_source_not_clean')
        if current!=target:
            # Source qualification owns all writes; detached maintenance cannot outlive it.
            self.command('git','-c','core.hooksPath=/dev/null','-c','maintenance.auto=false',
                         '-C',str(self.source),'merge','--ff-only',target)
        if self.identity()!=target:
            raise MigrationError('repair_revision_mismatch')


def failure(error, request):
    """Safe transport diagnostics: never include exception text or source rows."""
    from ..db import StoreError
    stage='before_journal'
    path=Path(request['root'])/'.schema-upgrade.json'
    if path.exists():
        try:
            value=decode(read_regular(path,limit=1048576))
            candidate=value.get('stage')
            stage=candidate if candidate in {'draining','stopping','installing','migrating','reconciling','complete'} else 'journal_unreadable'
        except Exception:
            stage='journal_unreadable'
    name=type(error).__name__
    result = dict(ok=False,error=error.code if isinstance(error,(MigrationError,StoreError)) else 'unexpected_migration_failure',
                  exception_type=name if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',name) else 'Exception',stage=stage)
    import sqlite3
    if isinstance(error, sqlite3.Error):
        # SQLite's structured identifiers contain no SQL, paths or stored data.
        # Exception text can contain SQL and paths, so transport only codes.
        code = getattr(error, 'sqlite_errorcode', None)
        label = getattr(error, 'sqlite_errorname', None)
        if type(code) is int:
            result['sqlite_errorcode'] = code
            if (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                result.update(error='migration_store_busy', retryable=True)
        if isinstance(label, str) and re.fullmatch(r'SQLITE_[A-Z0-9_]{1,80}', label):
            result['sqlite_errorname'] = label
    return result


def run(request):
    host=Host(request['root'],request['source'])
    if request['action']=='upgrade':
        from .upgrade import Upgrade
        return Upgrade(host).run(revision=request.get('source_revision'),apply=request.get('apply') is True,
                                 source_pack=request.get('source_pack'))
    raise MigrationError('unknown_migration_action')

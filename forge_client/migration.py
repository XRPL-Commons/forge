"""Deterministic migration CLI: preparation, staged inspection and one-way activation."""
import base64
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import uuid
import zipfile

from .bootstrap import Host, absolute_path
from .contracts import SetupError, require

# This bootstrap reads only locally supplied code and intent. The remote host
# never supplies commands. Temporary tools do not replace the running checkout.
BOOTSTRAP = r'''import base64,hashlib,json,pathlib,sys,tempfile,zipfile,io,os,subprocess
request=json.loads(sys.stdin.buffer.read(8*1024*1024))
raw=base64.b64decode(request.pop("bundle"),validate=True)
if hashlib.sha256(raw).hexdigest()!=request.pop("bundle_digest"): raise ValueError("bundle mismatch")
with tempfile.TemporaryDirectory(prefix="forge-migration-tool-") as directory:
 with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
  for name in bundle.namelist():
   p=pathlib.PurePosixPath(name)
   if p.is_absolute() or ".." in p.parts or p.parts[0]!="forge": raise ValueError("bad bundle")
  bundle.extractall(directory)
 sys.path.insert(0,directory)
 from forge.store.migration.host import run
 from forge.store.migration.snapshot import MigrationError
 from forge.store.db import StoreError,qualified_sqlite
 try:
  prepared=None
  if request["action"]!="prepare" and not qualified_sqlite():
   from forge.store.migration.runtime import inspect
   prepared=inspect(request["root"])
  if prepared:
   child="import json,sys;sys.path.insert(0,"+repr(directory)+");from forge.store.migration.host import run;from forge.store.migration.snapshot import MigrationError;from forge.store.db import StoreError\ntry: print(json.dumps({'ok':True,**run(json.load(sys.stdin))}))\nexcept (MigrationError,StoreError) as e: print(json.dumps({'ok':False,'error':e.code}))"
   completed=subprocess.run([sys.executable,"-I","-c",child],input=json.dumps(request),capture_output=True,text=True,timeout=1450,env={**os.environ,"LD_LIBRARY_PATH":prepared["library_directory"]})
   if completed.returncode or len(completed.stdout)>65536: raise MigrationError("qualified_migration_failed")
   print(completed.stdout.strip())
  else: print(json.dumps({"ok":True,**run(request)}))
 except (MigrationError,StoreError) as error: print(json.dumps({"ok":False,"error":error.code}))
'''


def bundle():
    import forge
    from forge.store.migration.source import source_files
    root=Path(forge.__file__).parent
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_files(root):
            archive.writestr('forge/'+path.relative_to(root).as_posix(),path.read_bytes())
    return output.getvalue()


def ssh(host, command):
    return ['ssh','-T','-a','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
            '-o','ClearAllForwardings=yes','-o','ForwardX11=no',host,command]


def call(args,action,**fields):
    # Reuse validation of explicit host/source/root intent.
    Host(args.ssh,args.source,args.root)
    resume = shlex.join(['forge', 'migrate', 'resume', '--ssh', args.ssh,
                         '--source', args.source, '--root', args.root, '--json'])
    from forge.store.migration.snapshot import digest
    code=bundle()
    request=dict(action=action,root=args.root,source=args.source,bundle=base64.b64encode(code).decode(),bundle_digest=digest(code),**fields)
    result=subprocess.run(ssh(args.ssh,'python3 -c '+shlex.quote(BOOTSTRAP)),input=json.dumps(request).encode(),capture_output=True,timeout=1500 if action in {'prepare','activate','resume'} else 300)
    require(result.returncode==0 and len(result.stdout)<=65536,'migration_host_failed',
            'The host operation failed. Resume maintenance with: ' + resume)
    value=json.loads(result.stdout)
    require(value.get('ok') is True,'migration_'+str(value.get('error','failed')),'Migration could not proceed: '+str(value.get('error','failed')))
    if value.get('next_command') == 'forge migrate resume':
        value['next_command'] = resume
    return value


def download(args,remote,local,manifest):
    # Fetch a fixed set of regular snapshot files, not a remotely supplied tar.
    script='''import pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1])
with tarfile.open(fileobj=sys.stdout.buffer,mode="w|") as archive:
 for path in sorted(root.iterdir()):
  if not path.is_file() or path.is_symlink(): raise ValueError("unsafe snapshot")
  archive.add(path,arcname=path.name,recursive=False)
'''
    import tarfile
    from forge.store.migration.snapshot import Snapshot
    from forge.store.db import _new_file,_fsync_directory
    local=Path(local).absolute()
    local.mkdir(mode=0o700)
    os.chmod(local,0o700)
    # Redirect the binary stream to a private file; no source contents enter logs.
    with tempfile.TemporaryFile() as stream:
        process=subprocess.run(ssh(args.ssh,'python3 -c '+shlex.quote(script)+' '+shlex.quote(remote)),stdout=stream,stderr=subprocess.PIPE,timeout=300)
        require(process.returncode==0,'snapshot_transfer_failed','Private snapshot transfer failed.')
        stream.seek(0)
        with tarfile.open(fileobj=stream,mode='r:') as archive:
            seen=set();total=0
            for member in archive:
                import re
                require(member.isfile() and re.fullmatch(r'(?:[0-9]{6}\.source|manifest\.json|sealed)',member.name)
                        and member.name not in seen and 0<=member.size<=512*1024*1024,'unsafe_snapshot','Invalid snapshot member.')
                total+=member.size
                require(total<=2*1024*1024*1024,'snapshot_too_large','Snapshot exceeds the supported transfer size.')
                seen.add(member.name)
                _new_file(local/member.name,archive.extractfile(member).read())
    _fsync_directory(local)
    source=Snapshot(local)
    require(source.id==manifest,'snapshot_identity_changed','Transferred snapshot identity does not match the capture receipt.')
    return source


def execute(args):
    from forge.store.migration.snapshot import MigrationError
    from forge.store.db import StoreError
    import sqlite3
    try:
        if args.migration_command in ('import','verify'):
            require(args.snapshot and args.destination,'migration_paths_required','Select --snapshot and --destination private directories.')
            from forge.store.migration.importer import Importer
            importer=Importer(args.snapshot,args.destination,batch=args.batch,create=args.migration_command=='import')
            return importer.run() if args.migration_command=='import' else importer.validate()
        require(args.ssh and args.source and args.root,'migration_host_required','Select --ssh, --source and --root for this host.')
        if args.migration_command in ('plan','resume'):
            return call(args,args.migration_command)
        if args.migration_command=='activate':
            require(args.apply,'migration_apply_required','One-way SQLite activation pauses new work, drains workers, verifies migration and restarts Forge. Supply --apply.')
            return call(args,'activate',apply=True)
        if args.migration_command=='prepare':
            require(args.apply,'migration_apply_required','Preparing the private SQLite runtime may install compiler dependencies. Supply --apply.')
            return call(args,'prepare')
        require(args.apply and args.snapshot,'migration_apply_required','Capture briefly stops Forge writers. Supply --apply and a new --snapshot private directory.')
        require(not Path(args.snapshot).exists() and not Path(args.snapshot).is_symlink(),'snapshot_exists','Choose a new private snapshot directory; capture never replaces an earlier copy.')
        plan=call(args,'plan')
        require(not plan['blockers'],'migration_blocked','Capture requires every job to finish verified cleanup.')
        remote=str(Path(absolute_path(args.root)).parent/('forge-migration-'+uuid.uuid4().hex))
        result=call(args,'capture',destination=remote,plan=plan['plan'])
        download(args,remote,args.snapshot,result['manifest'])
        return {**result,'snapshot':str(Path(args.snapshot).absolute()),'diagnostic_copy':remote,'services_restored':True}
    except (MigrationError, StoreError) as error:
        raise SetupError('migration_'+error.code,'Migration could not proceed: '+error.code) from None
    except (sqlite3.DatabaseError, KeyError, TypeError):
        raise SetupError('migration_mapping_failed','Migration could not validate its records. Private staging and the maintenance journal are retained; use forge migrate plan and resume to inspect and continue.') from None

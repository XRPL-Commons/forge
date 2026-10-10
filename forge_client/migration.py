"""Deterministic migration CLI: preparation, staged inspection and one-way activation."""
import base64
import io
from importlib.resources import files
import json
import os
import re
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
payload=sys.stdin.buffer.read(64*1024*1024+1)
if len(payload)>64*1024*1024: raise ValueError("request too large")
request=json.loads(payload)
raw=base64.b64decode(request.pop("bundle"),validate=True)
if hashlib.sha256(raw).hexdigest()!=request.pop("bundle_digest"): raise ValueError("bundle mismatch")
with tempfile.TemporaryDirectory(prefix="forge-migration-tool-") as directory:
 with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
  for name in bundle.namelist():
   p=pathlib.PurePosixPath(name)
   if p.is_absolute() or ".." in p.parts or p.parts[0]!="forge": raise ValueError("bad bundle")
  bundle.extractall(directory)
 sys.path.insert(0,directory)
 from forge.store.migration.host import run,failure
 from forge.store.migration.snapshot import MigrationError
 from forge.store.db import StoreError,qualified_sqlite
 try:
  prepared=None
  if request["action"]!="prepare" and not qualified_sqlite():
   from forge.store.migration.runtime import inspect
   prepared=inspect(request["root"])
  if prepared:
   child="import json,sys;sys.path.insert(0,"+repr(directory)+");from forge.store.migration.host import run,failure;request=json.load(sys.stdin)\ntry: print(json.dumps({'ok':True,**run(request)}))\nexcept Exception as e: print(json.dumps(failure(e,request)))"
   completed=subprocess.run([sys.executable,"-I","-c",child],input=json.dumps(request),capture_output=True,text=True,timeout=1450,env={**os.environ,"LD_LIBRARY_PATH":prepared["library_directory"]})
   if completed.returncode or len(completed.stdout)>65536: raise MigrationError("qualified_migration_failed")
   print(completed.stdout.strip())
  else: print(json.dumps({"ok":True,**run(request)}))
 except Exception as error: print(json.dumps(failure(error,request)))
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


SERVICE_USER_PATTERN = r'[a-z_][a-z0-9_-]{0,31}'


def service_user(args):
    user = getattr(args, 'service_user', None)
    require(user is None or isinstance(user, str) and re.fullmatch(SERVICE_USER_PATTERN, user),
            'invalid_service_user', 'Use the existing Unix service account name, such as forge.')
    return user


def transport(args, command, *, prepare_sqlite=False):
    """Both maintenance RPCs and snapshot reads use the same explicit account."""
    user = service_user(args)
    if user is not None:
        program = files('forge_client').joinpath('_service_host.py').read_text()
        command = ['python3', '-I', '-c', program, user, *(['--prepare-sqlite'] if prepare_sqlite else []), *command]
    return ssh(args.ssh, shlex.join(command))


def call(args,action,**fields):
    # Reuse validation of explicit host/source/root intent.
    Host(args.ssh,args.source,args.root)
    user = service_user(args)
    resume = shlex.join(['forge', 'migrate', 'prepare' if action=='prepare' else 'upgrade' if action=='upgrade' else 'resume', '--ssh', args.ssh,
                         '--source', args.source, '--root', args.root,
                         *(['--service-user', user] if user is not None else []), '--json'])
    if action == 'prepare':
        resume += ' --apply'
    if action=='upgrade' and getattr(args,'source_revision',None):
        resume += ' --apply --source-revision '+shlex.quote(args.source_revision)
    from forge.store.migration.snapshot import digest
    code=bundle()
    request=dict(action=action,root=args.root,source=args.source,bundle=base64.b64encode(code).decode(),bundle_digest=digest(code),**fields)
    result=subprocess.run(transport(args, ['python3', '-c', BOOTSTRAP], prepare_sqlite=action=='prepare'),input=json.dumps(request).encode(),capture_output=True,timeout=1500 if action in {'prepare','activate','resume','upgrade'} else 300)
    require(result.returncode==0 and len(result.stdout)<=65536,'migration_host_failed',
            ('The host operation failed. ' if user is None else 'Maintenance could not run as service user '+user+'. Verify that account and root or noninteractive sudo access. ')
            + 'Resume maintenance with: ' + resume)
    value=json.loads(result.stdout)
    if value.get('ok') is not True:
        import re
        token=lambda key,default: value[key] if isinstance(value.get(key),str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',value[key]) else default
        code,kind,stage=token('error','failed'),token('exception_type','Exception'),token('stage','unknown')
        if action == 'prepare':
            raise SetupError('migration_'+code, 'SQLite runtime preparation could not proceed: '+code+
                ' ('+kind+' at '+stage+'). Retry preparation with: '+resume)
        if action=='upgrade' and code=='drain_configuration_changed':
            raise SetupError('migration_'+code,'Runtime settings changed while maintenance was draining. The CLI kept the newer settings. '
                'Switch off agents explicitly before continuing, then resume with: '+resume)
        if action=='upgrade':
            raise SetupError('migration_'+code,'Schema upgrade could not proceed: '+code+' ('+kind+' at '+stage+'). Resume with: '+resume)
        repair=resume+' --accept-tool-update --source-revision '+(getattr(args,'source_revision',None) or '<full-commit-sha>')
        raise SetupError('migration_'+code,'Migration could not proceed: '+code+' ('+kind+' at '+stage+'). '
            +'Resume maintenance with: '+resume+'. For corrected tools before selection: '+repair)

    if value.get('next_command') in {'forge migrate resume', 'forge migrate upgrade'}:
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
    service_user(args)
    local=Path(local).absolute()
    local.mkdir(mode=0o700)
    os.chmod(local,0o700)
    # Redirect the binary stream to a private file; no source contents enter logs.
    with tempfile.TemporaryFile() as stream:
        process=subprocess.run(transport(args, ['python3', '-c', script, remote]),stdout=stream,stderr=subprocess.PIPE,timeout=300)
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
        user = service_user(args)
        require(user is None or args.migration_command not in ('import', 'verify'),
                'invalid_service_user', '--service-user applies only to remote migration operations.')
        repair=getattr(args,'accept_tool_update',False)
        revision=getattr(args,'source_revision',None)
        if args.migration_command=='upgrade':
            require(args.ssh and args.source and args.root,'migration_host_required','Select --ssh, --source and --root for this host.')
            require(not repair,'upgrade_options_invalid','Schema upgrades do not use --accept-tool-update.')
            import re
            require(not args.apply or isinstance(revision,str) and re.fullmatch(r'[0-9a-f]{40}',revision),'upgrade_revision_required','Supply the full release SHA with --source-revision when using --apply.')
            return call(args,'upgrade',apply=args.apply,source_revision=revision)
        require(not repair or args.migration_command=='resume','migration_repair_resume_required','Tool repair requires migrate resume.')
        require(not revision or args.migration_command in {'resume','activate'},'migration_repair_resume_required','Use --source-revision with initial migrate activate or an explicitly authorized migrate resume repair.')
        require(not repair or revision is not None,'migration_repair_revision_required','Select the exact corrected source with --source-revision and --accept-tool-update.')
        require(not revision or repair or args.migration_command=='activate','migration_repair_authorization_required','Supply --accept-tool-update to authorize this source revision.')
        if revision:
            import re
            require(re.fullmatch(r'[0-9a-f]{40}',revision) is not None,'migration_repair_revision_required','Use a full lowercase commit SHA for --source-revision.')
        if args.migration_command in ('import','verify'):
            require(args.snapshot and args.destination,'migration_paths_required','Select --snapshot and --destination private directories.')
            from forge.store.migration.importer import Importer
            importer=Importer(args.snapshot,args.destination,batch=args.batch,create=args.migration_command=='import')
            return importer.run() if args.migration_command=='import' else importer.validate()
        require(args.ssh and args.source and args.root,'migration_host_required','Select --ssh, --source and --root for this host.')
        if args.migration_command in ('plan','resume'):
            return call(args,args.migration_command,**({'accept_tool_update':True,'source_revision':revision} if repair else {}))
        if args.migration_command=='activate':
            require(args.apply,'migration_apply_required','One-way SQLite activation pauses new work, drains workers, verifies migration and restarts Forge. Supply --apply.')
            if revision:
                plan=call(args,'plan')
                require(plan.get('initial_setup') is True,'migration_initial_setup_required','Initial source upgrade requires a never-started legacy setup.')
                from .source import export
                pack=export(revision,previous=plan['source_revision']) if revision!=plan['source_revision'] else None
                return call(args,'activate',apply=True,previous_revision=plan['source_revision'],source_revision=revision,source_pack=pack)
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

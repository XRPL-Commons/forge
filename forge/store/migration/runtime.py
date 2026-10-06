"""Install a private, checksum-pinned SQLite library without switching services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from ..db import _new_file, _private, _fsync_directory
from .snapshot import MigrationError, digest, operation_lock, read_regular

# Same upstream source and checksum as the Linux regression-test runtime.
VERSION='3.51.3'
SOURCE_URL='https://www.sqlite.org/2026/sqlite-autoconf-3510300.tar.gz'
SOURCE_SHA='81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a'
DIRECTORY='sqlite-autoconf-3510300'
PROBE='''import json,sqlite3,tempfile,pathlib
with tempfile.TemporaryDirectory() as directory:
 db=sqlite3.connect(str(pathlib.Path(directory)/"qualification.sqlite3"))
 mode=db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
 db.execute("PRAGMA synchronous=FULL")
 db.execute("PRAGMA foreign_keys=ON")
 db.execute("PRAGMA trusted_schema=OFF")
 db.execute("CREATE TABLE proof(value TEXT CHECK(json_valid(value))) STRICT")
 db.execute("INSERT INTO proof VALUES(?)", ('{"qualified":true}',))
 db.commit()
 result=dict(version=sqlite3.sqlite_version,journal_mode=mode,synchronous=db.execute("PRAGMA synchronous").fetchone()[0],
  foreign_keys=db.execute("PRAGMA foreign_keys").fetchone()[0],integrity=db.execute("PRAGMA integrity_check").fetchone()[0])
 db.close()
 print(json.dumps(result))
'''


def library_digests(prefix):
    files=sorted(path for path in (prefix/'lib').glob('libsqlite3.so.*') if path.is_file() and not path.is_symlink())
    if not files:raise MigrationError('sqlite_runtime_library_missing')
    try:loaded=(prefix/'lib/libsqlite3.so.0').resolve(strict=True)
    except OSError:raise MigrationError('sqlite_runtime_library_missing') from None
    if loaded not in files:raise MigrationError('sqlite_runtime_identity_mismatch')
    return {path.name:digest(read_regular(path,limit=64*1024*1024)) for path in files}


def inspect(root, *, run=subprocess.run):
    target=Path(root)/'runtimes'/('sqlite-'+VERSION+'-'+SOURCE_SHA[:12])
    if not os.path.lexists(target):return None
    _private(target,directory=True)
    receipt=json.loads(read_regular(target/'qualification.json',limit=65536))
    if receipt.get('source_sha256')!=SOURCE_SHA or receipt.get('libraries')!=library_digests(target):
        raise MigrationError('sqlite_runtime_identity_mismatch')
    return dict(library_directory=str(target/'lib'),qualification=qualify(target,run=run))


def qualify(prefix, *, run=subprocess.run):
    env={**os.environ,'LD_LIBRARY_PATH':str(Path(prefix)/'lib')}
    result=run([sys.executable,'-I','-c',PROBE],env=env,capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise MigrationError('sqlite_runtime_qualification_failed')
    try:value=json.loads(result.stdout)
    except ValueError:raise MigrationError('sqlite_runtime_qualification_failed') from None
    if value!=dict(version=VERSION,journal_mode='wal',synchronous=2,foreign_keys=1,integrity='ok'):
        raise MigrationError('sqlite_runtime_qualification_failed')
    return value


def prepare(root, *, run=subprocess.run, download=urllib.request.urlopen):
    """Build privately, test WAL/FULL, then atomically publish an unused runtime.

    This does not select the database, change service environment, or replace
    system Python/SQLite. Rerunning qualifies the same immutable installation.
    """
    root=Path(root)
    if sys.platform!='linux':raise MigrationError('sqlite_runtime_requires_linux')
    directory=root/'runtimes'
    directory.mkdir(mode=0o700,exist_ok=True)
    _private(directory,directory=True)
    target=directory/('sqlite-'+VERSION+'-'+SOURCE_SHA[:12])
    with operation_lock(root,'.sqlite-runtime.lock'):
        if target.exists():
            return dict(status='runtime_ready',**inspect(root,run=run),changed=False,activation=False)
        if not shutil.which('cc') or not shutil.which('make'):
            # Dependency installation is part of the same explicit apply. It
            # never opens an interactive sudo/password prompt for an agent.
            prefix=[] if os.geteuid()==0 else ['sudo','-n']
            for args in (['apt-get','update'],['apt-get','install','-y','--no-install-recommends','build-essential']):
                result=run([*prefix,*args],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
                if result.returncode:raise MigrationError('sqlite_build_dependencies_unavailable')
        with tempfile.TemporaryDirectory(prefix='.sqlite-build-',dir=directory) as temporary:
            build=Path(temporary)
            try:
                with download(SOURCE_URL,timeout=60) as response:raw=response.read(20*1024*1024+1)
            except (OSError,ValueError):raise MigrationError('sqlite_download_failed') from None
            if len(raw)>20*1024*1024 or digest(raw)!=SOURCE_SHA:
                raise MigrationError('sqlite_source_digest_mismatch')
            archive=build/'source.tar.gz';_new_file(archive,raw)
            with tarfile.open(archive,'r:gz') as contents:
                for member in contents.getmembers():
                    name=Path(member.name)
                    if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0]!=DIRECTORY or not (member.isfile() or member.isdir()):
                        raise MigrationError('sqlite_source_archive_invalid')
                contents.extractall(build,filter='data')
            install=build/'install'
            log=build/'build.log'
            with log.open('wb') as output:
                for command in ([str(build/DIRECTORY/'configure'),'--prefix='+str(install),'--disable-static','--disable-readline'],
                                ['make','-j2'],['make','install']):
                    try:result=run(command,cwd=build/DIRECTORY,stdout=output,stderr=subprocess.STDOUT,timeout=300)
                    except (OSError,subprocess.TimeoutExpired):raise MigrationError('sqlite_runtime_build_failed') from None
                    if result.returncode:raise MigrationError('sqlite_runtime_build_failed')
            proof=qualify(install,run=run)
            install.chmod(0o700)
            _new_file(install/'qualification.json',json.dumps(dict(source_sha256=SOURCE_SHA,qualification=proof,libraries=library_digests(install)),sort_keys=True).encode())
            # Flush compiled library bytes before publishing the directory.
            for path in install.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    with path.open('rb') as stream:os.fsync(stream.fileno())
            directories=[path for path in install.rglob('*') if path.is_dir() and not path.is_symlink()]
            for path in sorted(directories,key=lambda path:len(path.parts),reverse=True):_fsync_directory(path)
            _fsync_directory(install)
            os.rename(install,target)
            _fsync_directory(directory)
            qualify(target,run=run)
            return dict(status='runtime_ready',library_directory=str(target/'lib'),qualification=proof,changed=True,activation=False)

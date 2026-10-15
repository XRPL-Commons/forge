"""Native, disposable qualification of the deployed worker boundary.

No installation state or repository is written. Every Docker resource carries a
unique canary identity. A success receipt is specific to the inspected image.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid

from .factory.worker_boundary import BoundaryError, WorkerBoundary
from .mirror.git_store import GitStore, environment, run_git
from .mirror.policy import Repository
from .mirror.service import Broker, SessionServer
from .registry_cache import ArchiveCache, Server as RegistryServer


@contextmanager
def no_credentials(repository):
    raise AssertionError('The canary must use its local fixture; no host fetch is permitted')
    yield  # pragma: no cover


class Fixture:
    def __init__(self, root):
        self.revoked = False
        self.receipts = []
        self.policy = dict(version=1, project_repository_id=1, canonical_repository='canary/project',
            revision='qualification', manifest_digest='a'*64, declared_repositories=[],
            organization_installations=[dict(account_id=10, installation_id=20, app_id=30)],
            denied_repositories=[], extractor_version=1)
        self.cache = GitStore(root / 'cache', credentials=no_credentials, refresh_seconds=3600)
        path = self.cache.path(1)
        run_git(['init', '--bare', str(path)])
        def git(*args, **kw): return run_git(['--git-dir', str(path), *args], **kw).decode().strip()
        blob = git('hash-object', '-w', '--stdin', data=b'boundary fixture\n')
        tree = git('mktree', data=f'100644 blob {blob}\tREADME.md\n'.encode())
        self.oid = git('commit-tree', tree, '-m', 'boundary fixture', env={**environment(),
            'GIT_AUTHOR_NAME':'Forge canary', 'GIT_AUTHOR_EMAIL':'canary@example.test',
            'GIT_COMMITTER_NAME':'Forge canary', 'GIT_COMMITTER_EMAIL':'canary@example.test'})
        git('update-ref', 'refs/heads/main', self.oid); git('symbolic-ref', 'HEAD', 'refs/heads/main')
        for key, value in [('http.receivepack','false'), ('http.getanyfile','false'), ('http.uploadpack','true')]:
            git('config', key, value)
        (path / 'git-daemon-export-ok').touch(); (path / 'forge-refreshed').touch()

    def session(self, identifier):
        return dict(revoked_at=time.time() if self.revoked else None, deadline=time.time()+600, policy=self.policy)

    def append_receipt(self, identifier, details, *, request_id=None):
        if request_id is not None and not any(row['receipt_id'] == request_id and row['outcome'] == 'started' for row in self.receipts):
            raise AssertionError('Canary receipt has no matching request')
        receipt_id = uuid.uuid4().hex
        self.receipts.append({**details, 'receipt_id': receipt_id, 'request_id': request_id})
        return receipt_id

    def broker(self):
        repository = Repository(1, 'canary/project', 10, 'Organization', 20, actual_access=True)
        return Broker('canary', self, self.cache, resolve=lambda name: repository,
            live_authority=lambda session: True,
            live_policy=lambda session: dict(organization_installations=self.policy['organization_installations'], denied_repositories=[]))


# Runs as UID 1000 in the same image and mounts as an ordinary worker. It performs
# real network requests; mocked HTTP responses cannot produce a success receipt.
WORKER = r'''
import errno,http.client,json,os,shutil,socket,ssl,subprocess,time,urllib.request
from pathlib import Path
cfg=json.loads(Path('/run/forge-input/assignment.json').read_text())
result={}
def run(argv):
    p=subprocess.run(argv,capture_output=True,text=True,timeout=75)
    assert p.returncode==0,(argv[0],p.returncode,p.stderr[-500:])
    return p.stdout.strip()
def denied(address,port):
    try:
        with socket.create_connection((address,port),timeout=2): pass
    except OSError:return
    raise AssertionError('unexpected direct network access: '+address)
def proxy_rejected(host):
    c=http.client.HTTPConnection('forge-egress',3128,timeout=10)
    c.request('CONNECT',host+':443');r=c.getresponse();assert r.status==403,(host,r.status);r.read();c.close()
# The proxy and frontend must be live before a negative observation is meaningful.
url='http://forge-git/github/canary/project.git'
run(['git','clone',url,'/work/repository'])
assert run(['git','-C','/work/repository','rev-parse','HEAD'])==cfg['oid']
run(['git','-C','/work/repository','fetch','origin'])
p=subprocess.run(['git','-C','/work/repository','push','origin','HEAD:refs/heads/forbidden'],capture_output=True,timeout=20)
assert p.returncode!=0
result['mirror_clone_fetch_read_only']=True
for address in cfg['github_ips']:denied(address,443)
denied(cfg['host_ip'],cfg['host_port']);denied(cfg['other_frontend_ip'],80)
proxy_rejected('github.com');proxy_rejected('api.github.com')
proxy_rejected('registry.npmjs.org')
result['github_host_cross_job_denied']=True
for hostname in ('github.com','api.github.com'):
    try:socket.getaddrinfo(hostname,443)
    except socket.gaierror:pass
    else:raise AssertionError('external DNS unexpectedly available')
result['external_dns_denied']=True
# TLS to a real approved provider succeeds without sending credentials.
c=http.client.HTTPSConnection('forge-egress',3128,timeout=25,context=ssl.create_default_context())
c.set_tunnel('api.openai.com',443);c.request('GET','/v1/models');r=c.getresponse()
assert r.status==401,('provider',r.status);r.read();c.close();result['provider_tls']=True
c=http.client.HTTPConnection('forge-registry',8081,timeout=10)
c.request('POST','/registry.npmjs.org/canary',b'{}');r=c.getresponse();assert r.status==403,r.status;r.read();c.close()
run(['npm','pack','is-number@7.0.0','--pack-destination','/work'])
run(['/usr/local/bin/python3','-m','pip','download','--disable-pip-version-check','--no-deps','--dest','/work','idna==3.10'])
result['registry_downloads_read_only']=True
try:Path('/run/forge-input/assignment.json').write_text('changed')
except OSError:pass
else:raise AssertionError('assignment input was writable')
assert shutil.which('gh') is None
assert not any(k in os.environ for k in ('GH_TOKEN','GITHUB_TOKEN','FORGE_GITHUB_TOKEN'))
assert not Path('/registry').exists()
result['immutable_input_no_github_credentials']=True
Path('/work/sentinel').write_text('stopped capture survives')
written=0
try:
    with open('/work/fill','wb',buffering=0) as stream:
        for _ in range(32):written+=stream.write(b'x'*1024**2)
except OSError as error:assert error.errno==errno.ENOSPC,error.errno
else:raise AssertionError('work exceeded quota')
assert written<=cfg['quota']
Path('/work/fill').unlink()
result['bounded_work_volume']=True
print(json.dumps(result,sort_keys=True))
'''


class Reachable(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b'canary')


def qualify(image, *, docker=None):
    if platform.system() != 'Linux':
        raise BoundaryError('Native boundary qualification requires the Linux Docker host')
    def call(args, **kwargs):
        return subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *args], capture_output=True, text=True,
                              check=kwargs.pop('check', True), timeout=kwargs.pop('timeout', 90), **kwargs)
    docker = docker or call
    boundary = WorkerBoundary(docker, image=image, work_bytes=16*1024**2)
    boundary.qualify()  # Refuse unsupported engine before any mutation.
    observed = json.loads(docker(['image','inspect',image]).stdout)[0]
    if observed['Id'] != image or observed.get('Config',{}).get('Labels',{}).get('org.forge.worker-boundary') != '2':
        raise BoundaryError('Qualification requires the exact worker-boundary v2 image ID')
    prefix = 'forge-qualification-' + uuid.uuid4().hex
    labels = {'forge.qualification': prefix}
    uplink = prefix+'-uplink'; boundary.uplink = uplink
    jobs = [dict(id=prefix+'-'+str(i),execution_fence=prefix) for i in range(2)]
    workers = []
    result = dict(image=image, engine=docker(['version','--format','{{.Server.Version}}']).stdout.strip())
    # Resolve externally on the trusted host; the worker has no external resolver.
    addresses = sorted({row[4][0] for host in ('github.com','api.github.com')
                        for row in socket.getaddrinfo(host,443,socket.AF_INET,socket.SOCK_STREAM)})
    servers=[];threads=[];specs=[]
    with tempfile.TemporaryDirectory(prefix='forge-qualification-',dir='/tmp') as temporary:
        root=Path(temporary);fixture=Fixture(root)
        try:
            registry_directory = root / 'registry-socket'
            registry_directory.mkdir(mode=0o700)
            registry_cache = ArchiveCache(root / 'registry-cache')
            registry_server = RegistryServer(registry_directory / 'registry.sock', registry_cache)
            servers.append(registry_server)
            thread = threading.Thread(target=registry_server.serve_forever, daemon=True)
            thread.start(); threads.append(thread)
            boundary.registry_directory = registry_directory
            docker(['network','create','--driver','bridge',*boundary.labels(labels),uplink])
            for i,job in enumerate(jobs):
                inputs=root/str(i)/'inputs';inputs.mkdir(parents=True,mode=0o755)
                socketpath=root/str(i)/'broker/session.sock'
                server=SessionServer(socketpath,fixture.broker());servers.append(server)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread)
                specs.append(boundary.prepare(job,input_directory=inputs,broker_socket_directory=socketpath.parent))
            with socketserver.ThreadingTCPServer(('0.0.0.0',0),Reachable) as host:
                thread=threading.Thread(target=host.serve_forever,daemon=True);thread.start()
                try:
                    net=boundary.inspect('network',uplink)
                    host_ip=net['IPAM']['Config'][0]['Gateway'];port=host.server_address[1]
                    second=boundary.inspect('container',specs[1].frontend)['NetworkSettings']['Networks'][specs[1].network]['IPAddress']
                    # Positive control: host listener and the other endpoint are live.
                    docker(['exec',specs[0].gateway,'/usr/local/bin/python3','-I','-c',
                            'import socket; s=socket.create_connection(('+repr(host_ip)+','+str(port)+'),5); assert s.recv(6)==b"canary"'])
                    docker(['exec',specs[1].gateway,'/usr/local/bin/python3','-I','-c',
                            'import socket,time\nfor i in range(50):\n try: socket.create_connection(("forge-git",80),1).close(); break\n except OSError:\n  if i==49: raise\n  time.sleep(.1)'])
                    docker(['exec',specs[0].gateway,'/usr/local/bin/python3','-I','-c',
                            'import socket,time\nfor i in range(50):\n try: socket.create_connection(("forge-git",80),1).close(); break\n except OSError:\n  if i==49: raise\n  time.sleep(.1)'])
                    note=dict(oid=fixture.oid,github_ips=addresses,host_ip=host_ip,host_port=port,
                              other_frontend_ip=second,quota=boundary.work_bytes)
                    target=root/'0/inputs/assignment.json';target.write_text(json.dumps(note));target.chmod(0o444)
                    worker=prefix+'-worker';workers.append((worker,jobs[0]));spec=specs[0]
                    env={**spec.environment,'HOME':'/work','XDG_CACHE_HOME':'/work/.cache','PIP_CACHE_DIR':'/work/.cache/pip'}
                    docker(['create','--pull=never','--name',worker,*boundary.labels(WorkerBoundary.identity(jobs[0])[1]),
                        '--read-only','--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges',
                        '--pids-limit','128','--memory','512m','--memory-swap','512m','--cpus','1',
                        '--tmpfs','/tmp:rw,nosuid,nodev,size=64m',*spec.worker_args,
                        *[item for k,v in env.items() for item in ('--env',k+'='+v)],
                        '--entrypoint','/usr/local/bin/python3',image,'-I','-c',WORKER])
                    docker(['start',worker]);exitcode=docker(['wait',worker],timeout=240).stdout.strip()
                    logs=docker(['logs',worker]);output=logs.stdout
                    if exitcode!='0':output += logs.stderr
                    if exitcode!='0':raise BoundaryError('Worker qualification failed: '+output[-1000:])
                    result['checks']=json.loads(output.strip().splitlines()[-1])
                    # A second physical job must reuse the first job's archive
                    # while the trusted cache's upstream is deliberately offline.
                    upstream = registry_cache.upstream
                    def offline(*args, **kwargs):
                        raise OSError('qualification cache miss with upstream disabled')
                    registry_cache.upstream = offline
                    hits = registry_cache.hits
                    try:
                        docker(['run','--rm','--pull=never','--read-only','--user','1000:1000',
                            '--cap-drop','ALL','--security-opt','no-new-privileges',
                            '--memory','128m','--pids-limit','32',*specs[1].worker_args,
                            '--entrypoint','/usr/local/bin/python3',image,'-I','-c',
                            'import urllib.request,pathlib; assert not pathlib.Path("/registry").exists(); '
                            'r=urllib.request.urlopen("http://forge-registry:8081/registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",timeout=30); '
                            'assert r.status==200 and len(r.read())>100'])
                    finally:
                        registry_cache.upstream = upstream
                    if registry_cache.hits != hits + 1:
                        raise BoundaryError('Second job did not reuse the shared package archive')
                    result['checks']['registry_cache_cross_job_offline_hit'] = True
                    docker(['cp',worker+':/work/sentinel',str(root/'sentinel')])
                    if (root/'sentinel').read_text()!='stopped capture survives':raise BoundaryError('Stopped capture failed')
                    result['checks']['stopped_capture']=True
                    fixture.revoked=True
                    docker(['exec',spec.gateway,'/usr/local/bin/python3','-I','-c',
                        'import urllib.request,urllib.error\ntry: urllib.request.urlopen("http://forge-git/github/canary/project.git/info/refs?service=git-upload-pack",timeout=10)\nexcept urllib.error.HTTPError as e: assert e.code==403\nelse: raise AssertionError("revoked session served")'])
                    result['checks']['revoked_session_denied']=True
                finally:
                    host.shutdown();thread.join()
        finally:
            for worker,job in workers:
                value=boundary.inspect('container',worker)
                if value:
                    boundary.owned(value,WorkerBoundary.identity(job)[1],container=True)
                    docker(['container','rm','--force',worker])
            for job in jobs:boundary.cleanup(job)
            value=boundary.inspect('network',uplink)
            if value:
                boundary.owned(value,labels);docker(['network','rm',uplink])
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join()
            if any(boundary.inspect('container',worker) for worker,_ in workers) or boundary.inspect('network',uplink):
                raise BoundaryError('Qualification cleanup is unconfirmed')
    result.update(status='qualified',cleanup='confirmed',observed_at=time.time())
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',required=True)
    args=parser.parse_args()
    try:result=qualify(args.image)
    except (BoundaryError,OSError,ValueError,subprocess.SubprocessError) as error:
        print(json.dumps(dict(status='failed',reason=str(error))));return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())

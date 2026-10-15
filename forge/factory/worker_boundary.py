"""Physical-job Docker boundary. Only the supervisor holds engine authority.

The bounded tmpfs volume stays mounted by the trusted frontend after worker exit
so stopped capture can read it. It is ephemeral across loss of every mount or a
host restart; a quota ceiling is not reserved memory and pages count toward the
writing worker's cgroup. Capture must finish before cleanup removes the keeper.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
import stat


class BoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundarySpec:
    network: str
    volume: str
    frontend: str
    gateway: str
    worker_args: tuple
    environment: dict


class WorkerBoundary:
    def __init__(self, call, *, image, uplink='forge-uplink', work_bytes=8*1024**3, registry_directory=None):
        if not isinstance(image,str) or not re.fullmatch(r'(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}',image) or type(work_bytes) is not int or not 16*1024**2 <= work_bytes <= 64*1024**3:
            raise ValueError('worker volume requires a bounded byte quota')
        self.call, self.image, self.uplink, self.work_bytes = call, image, uplink, work_bytes
        self.registry_directory = registry_directory

    @staticmethod
    def identity(job):
        identity = [job['id'], job['execution_fence']]
        if any(not isinstance(value, str) or not value or len(value)>256 for value in identity):
            raise ValueError('physical job and fence are required')
        digest = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        return 'forge-boundary-'+digest[:24], {'forge.execution': identity[0], 'forge.boundary': digest}

    def inspect(self, kind, name):
        field = 'Names' if kind == 'container' else 'Name'
        rows = self.call([kind, 'ls', *(['-a'] if kind == 'container' else []), '--format', '{{.'+field+'}}']).stdout.splitlines()
        names = [row.decode() if isinstance(row,bytes) else row for row in rows]
        if name not in names:
            return None
        values = json.loads(self.call([kind, 'inspect', name]).stdout)
        if len(values) != 1:
            raise BoundaryError('boundary inventory is ambiguous')
        return values[0]

    @staticmethod
    def owned(value, labels, *, container=False):
        actual = value.get('Config', {}).get('Labels', {}) if container else value.get('Labels', {})
        if any(actual.get(key) != expected for key, expected in labels.items()):
            raise BoundaryError('boundary resource belongs to a different physical job')

    def network(self, name, labels):
        value = self.inspect('network', name)
        if value is None:
            self.call(['network', 'create', '--driver', 'bridge', '--internal',
                '--opt', 'com.docker.network.bridge.gateway_mode_ipv4=isolated',
                *self.labels(labels), name])
            value = self.inspect('network', name)
        self.owned(value or {}, labels)
        if (value.get('Internal') is not True or value.get('EnableIPv6') is not False
                or value.get('Driver') != 'bridge'
                or value.get('Options', {}).get('com.docker.network.bridge.gateway_mode_ipv4') != 'isolated'):
            raise BoundaryError('worker network lacks isolated gateway support')

    @staticmethod
    def labels(labels):
        return [item for key, value in labels.items() for item in ('--label', key+'='+value)]

    def volume(self, name, labels):
        options = {'type':'tmpfs','device':'tmpfs','o':f'size={self.work_bytes},uid=1000,gid=1000,mode=0700,nosuid,nodev'}
        value = self.inspect('volume', name)
        if value is None:
            self.call(['volume', 'create', '--driver', 'local', *self.labels(labels),
                *[item for key,value in options.items() for item in ('--opt',key+'='+value)], name])
            value = self.inspect('volume', name)
        self.owned(value or {}, labels)
        if value.get('Driver') != 'local' or value.get('Options') != options:
            raise BoundaryError('worker volume quota changed')

    def endpoint(self, name, labels, network, command, mounts, aliases, *, uplink=False, user="1000:1000"):
        value = self.inspect('container', name)
        if value is None:
            self.call(['create', '--pull=never', '--name', name, *self.labels(labels),
                '--read-only','--user',user,'--cap-drop','ALL','--security-opt','no-new-privileges',
                '--pids-limit','64','--memory','256m','--memory-swap','256m','--cpus','0.25',
                '--log-driver','json-file','--log-opt','max-size=1m','--log-opt','max-file=1',
                '--network',network,*[item for alias in aliases for item in ('--network-alias',alias)],
                '--sysctl','net.ipv4.ip_forward=0',
                *[item for mount in mounts for item in ('--mount',mount)],
                '--entrypoint','/usr/local/bin/python3',self.image,'-I',*command])
            value = self.inspect('container', name)
        self.owned(value or {}, labels, container=True)
        config, host = value.get('Config', {}), value.get('HostConfig', {})
        if (config.get('Image') != self.image or config.get('Cmd') != ['-I',*command]
                or config.get('Entrypoint') != ['/usr/local/bin/python3'] or config.get('User') != user
                or not host.get('ReadonlyRootfs') or host.get('Privileged')
                or host.get('CapAdd') or 'ALL' not in host.get('CapDrop', [])
                or not any(option in {'no-new-privileges','no-new-privileges:true'} for option in host.get('SecurityOpt', []))
                or host.get('Memory') != 256*1024**2 or host.get('MemorySwap') != 256*1024**2
                or host.get('PidsLimit') != 64 or host.get('NanoCpus') != 250000000
                or host.get('PortBindings') or host.get('Sysctls', {}).get('net.ipv4.ip_forward') != '0'):
            raise BoundaryError('trusted endpoint configuration changed')
        actual_mounts = {(m['Destination'],m['Source'] if m['Type']=='bind' else m.get('Name'),m['RW']) for m in value.get('Mounts',[])}
        expected = set()
        for mount in mounts:
            parts = dict(part.split('=',1) for part in mount.split(',') if '=' in part)
            expected.add((parts['target'],parts['source'],False))
        if actual_mounts != expected:
            raise BoundaryError('trusted endpoint mounts changed')
        networks = set(value.get('NetworkSettings', {}).get('Networks', {}))
        if networks - {network, self.uplink} or network not in networks or not uplink and networks != {network}:
            raise BoundaryError('trusted endpoint network changed')
        if uplink and self.uplink not in networks:
            self.call(['network','connect',self.uplink,name])
        if not value.get('State', {}).get('Running'):
            if value.get('State', {}).get('Status') not in {'created','exited'}:
                raise BoundaryError('trusted endpoint state is unknown')
            self.call(['start',name])
        current=self.inspect('container',name)
        self.owned(current or {},labels,container=True)
        if current.get('State',{}).get('Running') is not True:
            raise BoundaryError('trusted endpoint is not running')

    @staticmethod
    def directory(path):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or not stat.S_ISDIR(path.stat().st_mode) or path.stat().st_mode & 0o022 or ',' in str(path):
            raise ValueError('boundary input must be an absolute private directory')
        return str(path)

    def qualify(self):
        raw=self.call(['version','--format','{{.Server.Version}}']).stdout
        version=raw.decode().strip() if isinstance(raw,bytes) else raw.strip()
        match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:[-+].*)?',version)
        if not match or tuple(map(int,match.groups()))<(28,0,0):
            raise BoundaryError('isolated worker networking requires Docker Engine 28 or newer')
        return True

    def prepare(self, job, *, input_directory, broker_socket_directory):
        self.qualify()
        name, labels = self.identity(job)
        input_directory = self.directory(input_directory)
        broker_socket_directory = self.directory(broker_socket_directory)
        network, volume, frontend, gateway = (name+suffix for suffix in ('-net','-work','-git','-gateway'))
        self.network(network, labels)
        self.volume(volume, labels)
        self.endpoint(frontend, labels, network, ['/opt/forge/mirror_frontend.py','--socket','/broker/session.sock'],
            ['type=bind,source='+broker_socket_directory+',target=/broker,readonly',
             'type=volume,source='+volume+',target=/retained-work,readonly,volume-nocopy'], ['forge-git'],
            user=f'{os.geteuid()}:{os.getegid()}')
        gateway_command, gateway_mounts = ['/opt/forge/gateway.py'], []
        if self.registry_directory is not None:
            directory = self.directory(self.registry_directory)
            gateway_command += ['--registry-socket', '/registry/registry.sock']
            gateway_mounts = ['type=bind,source='+directory+',target=/registry,readonly']
        self.endpoint(gateway, labels, network, gateway_command, gateway_mounts,
            ['forge-egress','forge-registry'], uplink=True, user=f'{os.geteuid()}:{os.getegid()}')
        environment = self.environment()
        return BoundarySpec(network,volume,frontend,gateway,('--network',network,'--dns','127.0.0.1',
            '--mount','type=volume,source='+volume+',target=/work,volume-nocopy',
            '--mount','type=bind,source='+input_directory+',target=/run/forge-input,readonly'),environment)

    @staticmethod
    def environment():
        return {'HTTP_PROXY':'http://forge-egress:3128','HTTPS_PROXY':'http://forge-egress:3128',
            'http_proxy':'http://forge-egress:3128','https_proxy':'http://forge-egress:3128',
            'NO_PROXY':'forge-git,forge-registry','no_proxy':'forge-git,forge-registry',
            'npm_config_registry':'http://forge-registry:8081/registry.npmjs.org/',
            'PIP_INDEX_URL':'http://forge-registry:8081/pypi.org/simple/', 'PIP_TRUSTED_HOST':'forge-registry'}

    def cleanup(self, job):
        name, labels = self.identity(job)
        # Verify every resource before removing any; never clean a colliding name.
        targets = [('container',name+'-gateway'),('container',name+'-git'),('volume',name+'-work'),('network',name+'-net')]
        present=[]
        for kind, target in targets:
            value=self.inspect(kind,target)
            if value is not None:
                self.owned(value,labels,container=kind=='container')
                present.append((kind,target))
        for kind,target in present:
            self.call([kind,'rm',*(['--force'] if kind=='container' else []),target])
        if any(self.inspect(kind,target) is not None for kind,target in targets):
            raise BoundaryError('boundary cleanup is not confirmed')
        return {'removed':True}

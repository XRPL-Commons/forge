#!/usr/bin/env python3
"""Local Docker Desktop canary; never select a remote or production daemon.

This qualifies work-volume retention/limits on existing images. Full isolated
routing additionally needs Docker >=28 and the integrated mirror/gateway image.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from forge.factory.worker_boundary import BoundaryError, WorkerBoundary


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);args=parser.parse_args()
    expected='unix://'+str(Path.home()/'.docker/run/docker.sock')
    observed=subprocess.check_output(['docker','context','inspect','desktop-linux','--format','{{.Endpoints.docker.Host}}'],text=True).strip()
    if observed!=expected:raise SystemExit('The canary requires the local Docker Desktop socket')
    def docker(argv,**kwargs):
        return subprocess.run(['docker','--host',expected,*argv],capture_output=True,check=kwargs.pop('check',True),timeout=kwargs.pop('timeout',60),**kwargs)
    image=json.loads(docker(['image','inspect',args.image]).stdout)[0]['Id']
    prefix='forge-boundary-canary-'+uuid.uuid4().hex[:12]
    volume,keeper,worker=prefix+'-work',prefix+'-keeper',prefix+'-worker'
    result={'image':image,'engine':docker(['version','--format','{{.Server.Version}}']).stdout.decode().strip()}
    try:
        WorkerBoundary(docker,image=image).qualify();result['isolated_engine']='supported'
    except BoundaryError:
        result['isolated_engine']='refused_unsupported_engine'
    base=['--pull=never','--network','none','--read-only','--user','1000:1000','--cap-drop','ALL',
          '--security-opt','no-new-privileges','--pids-limit','32','--memory','128m','--memory-swap','128m','--cpus','0.5',
          '--entrypoint','/usr/local/bin/python3']
    try:
        docker(['volume','create','--driver','local','--opt','type=tmpfs','--opt','device=tmpfs',
            '--opt','o=size=16777216,uid=1000,gid=1000,mode=0700,nosuid,nodev',volume])
        docker(['run','-d','--name',keeper,*base,'--mount','type=volume,source='+volume+',target=/retained,readonly,volume-nocopy',image,'-I','-c','import time; time.sleep(300)'])
        program="""import errno,json
from pathlib import Path
Path('/work/sentinel').write_text('stopped capture survives')
written=0
try:
    with open('/work/fill','wb',buffering=0) as stream:
        for _ in range(24):
            written+=stream.write(b'x'*1024**2)
except OSError as error:
    assert error.errno==errno.ENOSPC,error.errno
else:raise AssertionError('work volume exceeded quota')
assert written<=16777216,written
print(json.dumps({'quota_bytes':16777216,'written_before_full':written}))
"""
        output=docker(['run','--name',worker,*base,'--mount','type=volume,source='+volume+',target=/work,volume-nocopy',image,'-I','-c',program])
        result['quota']=json.loads(output.stdout)
        with tempfile.TemporaryDirectory(prefix='forge-boundary-capture-') as destination:
            docker(['cp',worker+':/work/sentinel',destination+'/sentinel'])
            assert Path(destination,'sentinel').read_text()=='stopped capture survives'
        result['stopped_capture']='passed'
        inspect=json.loads(docker(['container','inspect',worker]).stdout)[0]
        assert not inspect['State']['Running']
        assert set(inspect['NetworkSettings']['Networks'])=={'none'}
    finally:
        for container in (worker,keeper):docker(['container','rm','--force',container],check=False)
        docker(['volume','rm',volume],check=False)
    assert docker(['volume','inspect',volume],check=False).returncode!=0
    result['cleanup']='confirmed'
    # Exercise the gateway's actual HTTP parser and policy in the Linux image,
    # with no external network. The upstreams in these tests are fixture bytes.
    repository=Path(__file__).resolve().parents[1]
    gateway=str(repository/'deploy/runner/gateway.py')
    tests=str(repository/'tests/test_worker_gateway.py')
    output=docker(['run','--rm',*base,
        '--mount','type=bind,source='+gateway+',target=/probe/deploy/runner/gateway.py,readonly',
        '--mount','type=bind,source='+tests+',target=/probe/tests/test_worker_gateway.py,readonly',
        image,'-I','-c',"import runpy,unittest; module=runpy.run_path('/probe/tests/test_worker_gateway.py'); suite=unittest.defaultTestLoader.loadTestsFromTestCase(module['WorkerGatewayTests']); result=unittest.TextTestRunner().run(suite); raise SystemExit(not result.wasSuccessful())"])
    result['linux_gateway_http']='passed'
    result['isolated_routing']='requires_integrated_canary_on_supported_engine'
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()

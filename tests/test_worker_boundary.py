"""Boundary identity, retry and teardown cannot share a job's authority."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from forge.factory.worker_boundary import BoundaryError, WorkerBoundary


class Engine:
    def __init__(self):
        self.resources={kind:{} for kind in ('network','volume','container')};self.calls=[];self.version='29.0.0'
    def __call__(self,args,**kwargs):
        self.calls.append(args)
        def values(flag):return [args[i+1] for i,value in enumerate(args[:-1]) if value==flag]
        if args[0]=='version':return SimpleNamespace(stdout=self.version)
        kind=args[0] if args[0] in self.resources else 'container';operation=args[1] if args[0] in self.resources else args[0]
        if operation=='ls':return SimpleNamespace(stdout='\n'.join(self.resources[kind]))
        if operation=='inspect':return SimpleNamespace(stdout=json.dumps([self.resources[kind][args[2]]]))
        if operation=='create':
            name=values('--name')[0] if kind=='container' else args[-1]
            labels=dict(item.split('=',1) for item in values('--label'))
            if kind=='network':value=dict(Name=name,Labels=labels,Internal=True,EnableIPv6=False,Driver='bridge',Options=dict(item.split('=',1) for item in values('--opt')))
            elif kind=='volume':value=dict(Name=name,Labels=labels,Driver='local',Options=dict(item.split('=',1) for item in values('--opt')))
            else:
                image=args.index('--entrypoint')+2
                mounts=[]
                for raw in values('--mount'):
                    fields=dict(part.split('=',1) for part in raw.split(',') if '=' in part)
                    mounts.append(dict(Type=fields['type'],Destination=fields['target'],Source=fields['source'],Name=fields['source'],RW='readonly' not in raw.split(',')))
                value=dict(Config=dict(Image=args[image],Cmd=args[image+1:],Entrypoint=[args[image-1]],User=values('--user')[0],Labels=labels),
                    HostConfig=dict(ReadonlyRootfs=True,Privileged=False,CapAdd=None,CapDrop=['ALL'],SecurityOpt=['no-new-privileges'],Memory=256*1024**2,MemorySwap=256*1024**2,PidsLimit=64,NanoCpus=250000000,PortBindings={},Sysctls={'net.ipv4.ip_forward':'0'}),
                    Mounts=mounts,NetworkSettings={'Networks':{values('--network')[0]:{}}},State={'Status':'created','Running':False})
            self.resources[kind][name]=value
        elif operation=='connect':self.resources['container'][args[-1]]['NetworkSettings']['Networks'][args[-2]]={}
        elif operation=='start':self.resources['container'][args[-1]]['State']={'Status':'running','Running':True}
        elif operation=='rm':self.resources[kind].pop(args[-1])
        else:raise AssertionError(args)
        return SimpleNamespace(stdout='')


class WorkerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name).resolve();self.engine=Engine()
        self.boundary=WorkerBoundary(self.engine,image='sha256:'+'a'*64,work_bytes=16*1024**2)
        self.job={'id':'physical-one','execution_fence':'fence-one'}
    def prepare(self,job=None):
        return self.boundary.prepare(job or self.job,input_directory=self.path,broker_socket_directory=self.path)

    def test_restart_reuses_exact_boundary_and_stopped_work_keeper(self):
        first=self.prepare();count=len([c for c in self.engine.calls if 'create' in c])
        self.assertEqual(self.prepare(),first)
        self.assertEqual(len([c for c in self.engine.calls if 'create' in c]),count)
        frontend=self.engine.resources['container'][first.frontend]
        self.assertTrue(frontend['State']['Running'])
        self.assertEqual(set(frontend['NetworkSettings']['Networks']),{first.network})
        self.assertIn({'Destination':'/retained-work','Source':first.volume,'Name':first.volume,'Type':'volume','RW':False},frontend['Mounts'])
        self.assertIn('target=/run/forge-input,readonly',first.worker_args[-1])
        self.assertEqual(first.worker_args[first.worker_args.index('--dns')+1],'127.0.0.1')
        self.assertIn('size=16777216',self.engine.resources['volume'][first.volume]['Options']['o'])
        self.assertTrue(self.boundary.cleanup(self.job)['removed'])
        self.assertTrue(self.boundary.cleanup(self.job)['removed'])

    def test_distinct_physical_fences_never_share_work_or_network(self):
        first=self.prepare();second=self.prepare({**self.job,'execution_fence':'fence-two'})
        self.assertNotEqual(first.network,second.network);self.assertNotEqual(first.volume,second.volume)
        self.boundary.cleanup(self.job)
        self.assertIn(second.volume,self.engine.resources['volume'])
        self.assertIn(second.frontend,self.engine.resources['container'])

    def test_old_engine_is_refused_before_any_resource_creation(self):
        self.engine.version='23.0.5'
        with self.assertRaisesRegex(BoundaryError,'Docker Engine 28'):self.prepare()
        self.assertEqual(len(self.engine.calls),1)
        self.assertFalse(any(self.engine.resources.values()))

    def test_changed_quota_or_weakened_network_is_not_silently_reused(self):
        spec=self.prepare()
        self.engine.resources['network'][spec.network]['Options'].clear()
        with self.assertRaisesRegex(BoundaryError,'isolated'):self.prepare()
        self.engine.resources['network'][spec.network]['Options']['com.docker.network.bridge.gateway_mode_ipv4']='isolated'
        self.engine.resources['volume'][spec.volume]['Options']['o']='size=1g'
        with self.assertRaisesRegex(BoundaryError,'quota'):self.prepare()

    def test_endpoint_uplink_or_mount_tampering_is_refused(self):
        spec=self.prepare();saved=deepcopy(self.engine.resources['container'][spec.frontend])
        self.engine.resources['container'][spec.frontend]['NetworkSettings']['Networks']['host-control']={}
        with self.assertRaisesRegex(BoundaryError,'network'):self.prepare()
        self.engine.resources['container'][spec.frontend]=saved
        saved['Mounts'][0]['RW']=True
        with self.assertRaisesRegex(BoundaryError,'mounts'):self.prepare()

    def test_cleanup_checks_all_labels_before_removing_any_resources(self):
        spec=self.prepare();before=deepcopy(self.engine.resources)
        self.engine.resources['volume'][spec.volume]['Labels']['forge.boundary']='different'
        with self.assertRaisesRegex(BoundaryError,'different physical job'):self.boundary.cleanup(self.job)
        self.assertEqual(set(self.engine.resources['container']),set(before['container']))

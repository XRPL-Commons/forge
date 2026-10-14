import hashlib
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zlib

from forge.factory.source_inputs import create, digest
from forge.publication_artifacts import BaseObjects, Limits, PublicationClaim, inspect_output, _pack_bounds


class PublicationArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.git('init','--quiet');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid')
        (self.repo/'keep.txt').write_text('base\n');self.git('add','.');self.git('commit','--quiet','-m','base')
        self.base=self.git('rev-parse','HEAD').strip()
        self.snapshot=self.root/'base.git';self.git('clone','--quiet','--bare',str(self.repo),str(self.snapshot))
        self.source=create(repository_id=1,canonical_repository='owner/repo',base_oid=self.base,input_oids=[],
            manifest={'base_oid':self.base},dependency_policy_digest='1'*64,notes={})
        self.objects=BaseObjects(1,digest(self.source),'sha1',(self.base,),self.snapshot)

    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True,env={**os.environ,'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'})

    def output(self, *, limits=Limits(), source=None, objects=None):
        self.git('add','.');self.git('commit','--quiet','--allow-empty','-m','work')
        head=self.git('rev-parse','HEAD').strip();bundle=self.root/'output.bundle'
        self.git('bundle','create',str(bundle),'HEAD','^'+self.base)
        return inspect_output(bundle.read_bytes(),head,source or self.source,objects or self.objects,limits=limits)

    def test_exact_binary_tree_and_input_identity(self):
        (self.repo/'binary').write_bytes(b'\x00\xff\n')
        output=self.output()
        self.assertEqual(output.tree_oid,self.git('rev-parse','HEAD^{tree}').strip())
        self.assertEqual(output.changes[0].content,b'\x00\xff\n')
        self.assertEqual(output.identity()['source_input_digest'],digest(self.source))
        self.assertEqual(output.graphql_changes()['additions'][0]['contents'],'AP8K')

    def test_modes_symlinks_and_gitlink_are_preserved_as_facts(self):
        (self.repo/'run').write_text('#!/bin/sh\n');os.chmod(self.repo/'run',0o755)
        os.symlink('keep.txt',self.repo/'link')
        (self.repo/'submodule').mkdir()
        self.git('update-index','--add','--cacheinfo','160000,'+self.base+',submodule')
        output=self.output()
        self.assertEqual({c.path:c.new_mode for c in output.changes},{'link':'120000','run':'100755','submodule':'160000'})
        self.assertEqual(next(c for c in output.changes if c.path=='link').content,b'keep.txt')
        with self.assertRaises(PublicationClaim) as error:output.graphql_changes()
        self.assertEqual(error.exception.code,'file_mode_unsupported')

    def test_protected_paths_and_test_facts_do_not_gate_output(self):
        (self.repo/'.github/workflows').mkdir(parents=True);(self.repo/'.github/workflows/test.yml').write_text('on: workflow_dispatch\n')
        (self.repo/'CODEOWNERS').write_text('* @owner\n');(self.repo/'test_case.py').write_text('pytest.skip("later")\n')
        output=self.output()
        self.assertEqual(output.facts['protected_paths'],['.github/workflows/test.yml','CODEOWNERS'])
        self.assertEqual(output.facts['added_test_markers'],[{'path':'test_case.py','markers':['skip']}])
        self.assertEqual(len(output.graphql_changes()['additions']),3)

    def test_nested_lfs_attributes_are_a_typed_claim(self):
        (self.repo/'nested').mkdir();(self.repo/'nested/.gitattributes').write_text('*.bin filter=lfs diff=lfs\n')
        with self.assertRaises(PublicationClaim) as error:self.output()
        self.assertEqual(error.exception.payload(),{'code':'git_lfs_unsupported','paths':['nested/.gitattributes']})

    def test_file_limit_is_distinct_from_review_facts(self):
        (self.repo/'large').write_bytes(b'x'*100)
        with self.assertRaises(PublicationClaim) as error:self.output(limits=Limits(file_bytes=20))
        self.assertEqual(error.exception.code,'object_size_limit')

    def test_snapshot_cannot_substitute_repository_identity(self):
        wrong=BaseObjects(2,digest(self.source),'sha1',(self.base,),self.snapshot)
        with self.assertRaises(PublicationClaim) as error:self.output(objects=wrong)
        self.assertEqual(error.exception.code,'base_objects_mismatch')

    def test_missing_assigned_input_cannot_publish(self):
        source={**self.source,'input_oids':['c'*40]}
        objects=BaseObjects(1,digest(source),'sha1',(self.base,'c'*40),self.snapshot)
        with self.assertRaises(PublicationClaim) as error:self.output(source=source,objects=objects)
        self.assertEqual(error.exception.code,'assigned_input_missing')

    def test_filename_not_representable_by_api_is_refused(self):
        (self.repo/'bad\npath').write_text('content')
        with self.assertRaises(PublicationClaim) as error:self.output()
        self.assertEqual(error.exception.code,'path_unsupported')

    def test_file_directory_replacement_is_exact(self):
        (self.repo/'keep.txt').unlink();(self.repo/'keep.txt').mkdir();(self.repo/'keep.txt/child').write_text('child\n')
        output=self.output()
        self.assertEqual([(c.path,c.old_mode,c.new_mode) for c in output.changes],
            [('keep.txt','100644',None),('keep.txt/child',None,'100644')])

    def test_compressed_object_size_is_bounded_before_git_indexing(self):
        data=b'a'*100000;size=len(data);header=bytearray([(3<<4)|(size&15)]);size>>=4
        while size:
            header[-1]|=128;header.append(size&127);size>>=7
        pack=b'PACK'+struct.pack('!II',2,1)+bytes(header)+zlib.compress(data)
        pack+=hashlib.sha1(pack).digest()
        with self.assertRaises(PublicationClaim) as error:_pack_bounds(pack,Limits(object_bytes=1024))
        self.assertEqual(error.exception.code,'object_size_limit')

    def test_pack_checksum_is_verified_before_git(self):
        with self.assertRaises(PublicationClaim) as error:_pack_bounds(b'PACK'+struct.pack('!II',2,0)+b'0'*20,Limits())
        self.assertEqual(error.exception.code,'bundle_invalid')

    def test_rework_delta_uses_pinned_parent_tree(self):
        (self.repo/'removed-on-rework').write_text('old work')
        self.git('add','.');self.git('commit','--quiet','-m','previous')
        parent=self.git('rev-parse','HEAD').strip()
        self.git('--git-dir='+str(self.snapshot),'fetch','--quiet',str(self.repo),parent)
        source=create(repository_id=1,canonical_repository='owner/repo',base_oid=self.base,input_oids=[parent],
            manifest={'base_oid':self.base},dependency_policy_digest='1'*64,notes={})
        objects=BaseObjects(1,digest(source),'sha1',(self.base,parent),self.snapshot)
        (self.repo/'removed-on-rework').unlink();(self.repo/'new').write_text('new work')
        output=self.output(source=source,objects=objects)
        delta=output.graphql_changes(parent)
        self.assertEqual(delta['deletions'],[{'path':'removed-on-rework'}])
        self.assertEqual(delta['additions'],[{'path':'new','contents':'bmV3IHdvcms='}])

    def test_preparation_bundle_returns_pack_only_after_full_validation(self):
        from forge.publication_artifacts import inspect_preparation_bundle
        (self.repo/'new').write_text('new');output=self.output()
        content=(self.root/'output.bundle').read_bytes()
        pack=inspect_preparation_bundle(content,output.head_oid,1,self.base,[],self.objects)
        self.assertTrue(pack.startswith(b'PACK'))
        with self.assertRaises(PublicationClaim):inspect_preparation_bundle(content,'f'*40,1,self.base,[],self.objects)

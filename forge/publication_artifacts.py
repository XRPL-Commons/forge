"""Bounded Git output verification; no checkout, filters or repository execution."""
from dataclasses import dataclass
import difflib
import hashlib
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time
import zlib

from .factory import source_inputs


@dataclass(frozen=True)
class Limits:
    bundle_bytes: int = 16 * 1024 * 1024
    object_bytes: int = 16 * 1024 * 1024
    decoded_bytes: int = 128 * 1024 * 1024
    file_bytes: int = 4 * 1024 * 1024
    changed_files: int = 1000
    objects: int = 100000
    commits: int = 10000
    paths: int = 100000
    path_bytes: int = 8 * 1024 * 1024
    seconds: int = 30


class PublicationClaim(ValueError):
    """A typed transport/content limitation, never a worker budget disposition."""
    def __init__(self, code, *, paths=(), limit=None):
        self.code, self.paths, self.limit = code, tuple(paths)[:20], limit
        super().__init__(code)

    def payload(self):
        return dict(code=self.code, paths=list(self.paths), **({'limit': self.limit} if self.limit is not None else {}))


@dataclass(frozen=True)
class BaseObjects:
    repository_id: int
    source_input_digest: str
    object_format: str
    pinned_oids: tuple
    git_dir: Path


@dataclass(frozen=True)
class Change:
    path: str
    old_mode: str | None
    old_oid: str | None
    new_mode: str | None
    new_oid: str | None
    content: bytes | None

    def identity(self):
        return dict(path=self.path, old_mode=self.old_mode, old_oid=self.old_oid,
                    new_mode=self.new_mode, new_oid=self.new_oid,
                    bytes=len(self.content) if self.content is not None else 0)


@dataclass(frozen=True)
class VerifiedOutput:
    source: dict
    bundle_digest: str
    bundle_size: int
    head_oid: str
    base_tree_oid: str
    tree_oid: str
    changes: tuple[Change, ...]
    facts: dict
    trees: tuple = ()
    parent_changes: tuple = ()

    def identity(self):
        return dict(version=1, source_input=self.source, source_input_digest=source_inputs.digest(self.source),
                    bundle_digest=self.bundle_digest, bundle_size=self.bundle_size, head_oid=self.head_oid,
                    base_tree_oid=self.base_tree_oid, tree_oid=self.tree_oid,
                    changes=[change.identity() for change in self.changes], facts=self.facts,
                    facts_digest=source_inputs.digest(self.facts),
                    parent_changes=[dict(parent_oid=oid,changes=[c.identity() for c in changes]) for oid,changes in self.parent_changes])

    def digest(self):
        return source_inputs.digest(self.identity())

    def graphql_changes(self,parent_oid=None):
        changes=self.changes if parent_oid in (None,self.source['base_oid']) else dict(self.parent_changes).get(parent_oid)
        if changes is None:raise PublicationClaim('publication_parent_tree_unsupported')
        # The GraphQL API cannot represent modes. Leave unchanged special entries
        # untouched, but do not silently flatten a changed executable or symlink.
        unsupported=[change.path for change in changes
                     if change.new_mode not in (None,'100644') or change.old_mode not in (None,'100644')]
        if unsupported:raise PublicationClaim('file_mode_unsupported', paths=unsupported)
        import base64
        return dict(additions=[dict(path=c.path,contents=base64.b64encode(c.content).decode())
                               for c in changes if c.new_mode is not None],
                    deletions=[dict(path=c.path) for c in changes if c.new_mode is None])


def _pack_bounds(pack, limits):
    """Inspect inflated object/delta sizes before index-pack can allocate them."""
    if len(pack)<32 or pack[:4]!=b'PACK':raise PublicationClaim('bundle_invalid')
    version,count=struct.unpack('!II',pack[4:12])
    if version not in (2,3) or count>limits.objects:raise PublicationClaim('object_limit',limit=limits.objects)
    pos,total=12,0
    for _ in range(count):
        if pos>=len(pack):raise PublicationClaim('bundle_invalid')
        byte=pack[pos];pos+=1;kind=(byte>>4)&7;size=byte&15;shift=4
        while byte&128:
            if pos>=len(pack) or shift>63:raise PublicationClaim('bundle_invalid')
            byte=pack[pos];pos+=1;size|=(byte&127)<<shift;shift+=7
        if kind not in (1,2,3,4,6,7):raise PublicationClaim('bundle_invalid')
        if size>limits.object_bytes:raise PublicationClaim('object_size_limit',limit=limits.object_bytes)
        if kind==6:
            while True:
                if pos>=len(pack):raise PublicationClaim('bundle_invalid')
                byte=pack[pos];pos+=1
                if not byte&128:break
        elif kind==7:pos+=20
        decoder=zlib.decompressobj();actual=0;prefix=b''
        try:
            while not decoder.eof:
                chunk=pack[pos:pos+65536]
                if not chunk:raise PublicationClaim('bundle_invalid')
                output=decoder.decompress(chunk,min(65536,size-actual+1))
                consumed=len(chunk)-len(decoder.unconsumed_tail)-len(decoder.unused_data)
                if not consumed and not output:raise PublicationClaim('bundle_invalid')
                pos+=consumed;actual+=len(output)
                if actual>size:raise PublicationClaim('bundle_invalid')
                prefix=(prefix+output)[:32]
        except zlib.error:raise PublicationClaim('bundle_invalid') from None
        if actual!=size:raise PublicationClaim('bundle_invalid')
        reconstructed=size
        if kind in (6,7):
            offset=0;sizes=[]
            for _ in range(2):
                value=0;shift=0
                while True:
                    if offset>=len(prefix) or shift>63:raise PublicationClaim('bundle_invalid')
                    byte=prefix[offset];offset+=1;value|=(byte&127)<<shift;shift+=7
                    if not byte&128:break
                sizes.append(value)
            reconstructed=max(sizes)
            if reconstructed>limits.object_bytes:raise PublicationClaim('object_size_limit',limit=limits.object_bytes)
        total+=max(size,reconstructed)
        if total>limits.decoded_bytes:raise PublicationClaim('decoded_size_limit',limit=limits.decoded_bytes)
    if pos!=len(pack)-20 or hashlib.sha1(pack[:-20]).digest()!=pack[-20:]:raise PublicationClaim('bundle_invalid')


def _header(content, head, source, limits):
    if not isinstance(content,bytes) or not 0<len(content)<=limits.bundle_bytes:
        raise PublicationClaim('bundle_size_limit',limit=limits.bundle_bytes)
    header,separator,pack=content.partition(b'\n\n')
    if not separator or len(header)>65536:raise PublicationClaim('bundle_invalid')
    try:lines=header.decode('ascii').splitlines()
    except UnicodeError:raise PublicationClaim('bundle_invalid') from None
    if not lines or lines.pop(0) not in ('# v2 git bundle','# v3 git bundle'):raise PublicationClaim('bundle_invalid')
    pins={source['base_oid'],*source['input_oids']};tips=[]
    for line in lines:
        if line.startswith('-'):
            if line[1:].split(' ',1)[0] not in pins:raise PublicationClaim('bundle_prerequisite_mismatch')
        elif line.startswith('@'):
            if line!='@object-format=sha1':raise PublicationClaim('object_format_unsupported')
        else:tips.append(line.split(' ',1))
    if tips!=[[head,'HEAD']]:raise PublicationClaim('bundle_head_mismatch')
    _pack_bounds(pack,limits)
    return pack


def inspect_output(content, head, source, base_objects, *, limits=Limits()):
    source=source_inputs.validate(source)
    if source['object_format']!='sha1':raise PublicationClaim('object_format_unsupported')
    if not isinstance(head,str) or not re.fullmatch('[0-9a-f]{40}',head):raise PublicationClaim('bundle_head_mismatch')
    pins={source['base_oid'],*source['input_oids']}
    if (base_objects.repository_id!=source['repository_id'] or base_objects.source_input_digest!=source_inputs.digest(source)
            or base_objects.object_format!=source['object_format'] or not pins.issubset(base_objects.pinned_oids)):
        raise PublicationClaim('base_objects_mismatch')
    objects=Path(base_objects.git_dir).resolve()/'objects'
    if not objects.is_dir() or any(c in str(objects) for c in ':\n\r'):
        raise PublicationClaim('base_objects_invalid')
    pack=_header(content,head,source,limits)
    env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL','TMPDIR') if key in os.environ}
    env.update(GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1',GIT_NO_REPLACE_OBJECTS='1',
               GIT_ALTERNATE_OBJECT_DIRECTORIES=str(objects),GIT_TERMINAL_PROMPT='0')
    deadline=time.monotonic()+limits.seconds
    with tempfile.TemporaryDirectory(prefix='forge-publication-') as directory:
        def git(*args,data=None):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise PublicationClaim('verification_timeout')
            try:
                result=subprocess.run(['git','-C',directory,*args],input=data,env=env,capture_output=True,timeout=remaining)
            except (OSError,subprocess.TimeoutExpired):raise PublicationClaim('verification_unavailable') from None
            if result.returncode:raise PublicationClaim('git_object_invalid')
            return result.stdout
        git('init','--bare','--quiet','--object-format=sha1')
        git('index-pack','--stdin','--fix-thin','--max-input-size='+str(limits.bundle_bytes),data=pack)
        read_bytes=0
        def read(oid,kind,cap=limits.object_bytes):
            nonlocal read_bytes
            if git('cat-file','-t',oid).strip()!=kind.encode():raise PublicationClaim('git_object_type')
            size=int(git('cat-file','-s',oid))
            if size>cap:raise PublicationClaim('object_size_limit',limit=cap)
            read_bytes+=size
            if read_bytes>limits.decoded_bytes:raise PublicationClaim('decoded_size_limit',limit=limits.decoded_bytes)
            return git('cat-file',kind,oid)
        def commit(oid):
            raw=read(oid,'commit',65536).split(b'\n\n',1)[0].splitlines()
            trees=[line[5:].decode('ascii') for line in raw if line.startswith(b'tree ')]
            parents=[line[7:].decode('ascii') for line in raw if line.startswith(b'parent ')]
            if len(trees)!=1 or any(not re.fullmatch('[0-9a-f]{40}',oid) for oid in [*trees,*parents]):raise PublicationClaim('git_commit_invalid')
            return trees[0],parents
        base_tree,_=commit(source['base_oid']);target_tree,_=commit(head)
        reached=set();seen=set();todo=[head]
        while todo:
            current=todo.pop()
            if current in pins:reached.add(current)
            if current in seen or current==source['base_oid']:continue
            seen.add(current)
            if len(seen)>limits.commits:raise PublicationClaim('history_limit',limit=limits.commits)
            _,parents=commit(current)
            if not parents:raise PublicationClaim('base_ancestry_missing')
            todo.extend(parents)
        if reached!=pins:raise PublicationClaim('assigned_input_missing')
        def tree_entries(root):
            trees={};entries={};todo=[('',root,0)];path_size=0
            while todo:
                prefix,oid,depth=todo.pop()
                if depth>64:raise PublicationClaim('tree_depth_limit',limit=64)
                raw=read(oid,'tree');offset=0;names=set();direct=[]
                while offset<len(raw):
                    split=raw.find(b' ',offset);end=raw.find(b'\0',split+1)
                    if split<0 or end<0 or end+21>len(raw):raise PublicationClaim('git_tree_invalid')
                    mode=raw[offset:split].decode('ascii');name_bytes=raw[split+1:end]
                    try:name=name_bytes.decode('utf-8')
                    except UnicodeError:raise PublicationClaim('path_encoding_unsupported') from None
                    if (name in ('','.','..') or '/' in name or '\\' in name or name.casefold()=='.git'
                            or any(ord(c)<32 for c in name) or name in names):raise PublicationClaim('path_unsupported')
                    names.add(name);path=prefix+name;path_size+=len(path.encode())
                    if path_size>limits.path_bytes:raise PublicationClaim('path_size_limit',limit=limits.path_bytes)
                    child=raw[end+1:end+21].hex();offset=end+21
                    direct.append(dict(path=name,mode='040000' if mode=='40000' else mode,type='tree' if mode=='40000' else 'commit' if mode=='160000' else 'blob',sha=child))
                    if mode=='40000':todo.append((path+'/',child,depth+1))
                    elif mode in ('100644','100755','120000','160000'):entries[path]=(mode,child)
                    else:raise PublicationClaim('file_mode_unsupported',paths=[path])
                    if len(entries)+len(todo)>limits.paths:raise PublicationClaim('tree_path_limit',limit=limits.paths)
                trees[oid]=tuple(direct)
            return entries,trees
        before,old_trees=tree_entries(base_tree);after,new_trees=tree_entries(target_tree)
        for path,(mode,oid) in after.items():
            if path.rsplit('/',1)[-1]=='.gitattributes' and mode in ('100644','100755'):
                attributes=read(oid,'blob',limits.file_bytes).decode('utf-8',errors='replace')
                if any(not line.lstrip().startswith('#') and re.search(r'(^|\s)filter=lfs($|\s)',line) for line in attributes.splitlines()):
                    raise PublicationClaim('git_lfs_unsupported',paths=[path])
        paths=sorted(path for path in set(before)|set(after) if before.get(path)!=after.get(path))
        if len(paths)>limits.changed_files:raise PublicationClaim('changed_file_limit',limit=limits.changed_files)
        changes=[];protected=[];deleted_tests=[];markers=[]
        for path in paths:
            old_mode,old_oid=before.get(path,(None,None));new_mode,new_oid=after.get(path,(None,None))
            data=read(new_oid,'blob',limits.file_bytes) if new_mode and new_mode!='160000' else None
            changes.append(Change(path,old_mode,old_oid,new_mode,new_oid,data))
            if (path.startswith('.github/workflows/') or path.rsplit('/',1)[-1]=='CODEOWNERS'
                    or path in ('forge.json','forge.yml','forge.yaml') or path.startswith(('.forge/','.github/forge'))):protected.append(path)
            if not new_mode and (re.search(r'(^|/)(tests?|__tests__)/',path) or re.search(r'(^|/)(test_|.*[._](test|spec)\.)',path)):
                deleted_tests.append(path)
            if data is not None and b'\0' not in data:
                previous=read(old_oid,'blob',limits.file_bytes) if old_mode in ('100644','100755','120000') else b''
                added=[line[1:] for line in difflib.unified_diff(previous.decode('utf-8',errors='replace').splitlines(),data.decode('utf-8',errors='replace').splitlines()) if line.startswith('+') and not line.startswith('+++')]
                found=sorted({match.group(0) for line in added for match in re.finditer(r'\b(?:skip|only|xit)\b',line)})
                if found:markers.append(dict(path=path,markers=found))
        facts=dict(protected_paths=protected,deleted_tests=deleted_tests,added_test_markers=markers)
        parent_changes=[]
        for parent_oid in source['input_oids']:
            if parent_oid==source['base_oid']:continue
            parent_tree,_=commit(parent_oid);parent_entries,_=tree_entries(parent_tree)
            parent_paths=sorted(path for path in set(parent_entries)|set(after) if parent_entries.get(path)!=after.get(path))
            if len(parent_paths)>limits.changed_files:raise PublicationClaim('changed_file_limit',limit=limits.changed_files)
            delta=[]
            for path in parent_paths:
                old_mode,old_oid=parent_entries.get(path,(None,None));new_mode,new_oid=after.get(path,(None,None))
                data=read(new_oid,'blob',limits.file_bytes) if new_mode and new_mode!='160000' else None
                delta.append(Change(path,old_mode,old_oid,new_mode,new_oid,data))
            parent_changes.append((parent_oid,tuple(delta)))
        return VerifiedOutput(source,hashlib.sha256(content).hexdigest(),len(content),head,base_tree,target_tree,tuple(changes),facts,
            tuple((oid,entries) for oid,entries in reversed(list(new_trees.items())) if oid not in old_trees),tuple(parent_changes))


def inspect_preparation_bundle(content,head,repository_id,base_oid,input_oids,base_objects,*,limits=Limits()):
    """Validate retained child input before computing its final dependency policy.

    The private identity is exclusively for object verification. It must never
    be assigned to a worker or used as publication authority.
    """
    purpose=source_inputs.digest({'purpose':'source-preparation-object-validation','version':1})
    source=source_inputs.create(repository_id=repository_id,canonical_repository='preparation/input',
        base_oid=base_oid,input_oids=input_oids,manifest={'purpose':'source-preparation-object-validation'},
        dependency_policy_digest=purpose,notes={'purpose':'source-preparation-object-validation'})
    handle=BaseObjects(base_objects.repository_id,source_inputs.digest(source),base_objects.object_format,
                       tuple(base_objects.pinned_oids),base_objects.git_dir)
    inspect_output(content,head,source,handle,limits=limits)
    return _header(content,head,source,limits)

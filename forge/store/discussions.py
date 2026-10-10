"""Typed discussion writes; messages, launches, leases and evidence are separate rows."""
import hashlib
import json
import time
import uuid

from .executions import provider_available
from .models import IdempotencyConflict, TransitionError, VersionConflict
from .runtime import writable
from .supporting_queries import message
from .transitions import _json


class DiscussionStore:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    @staticmethod
    def _issue(db,key):
        try:
            repository,number = map(int,key.split(':'))
        except (ValueError,AttributeError):
            raise TransitionError('discussion requires a repository and issue identity') from None
        row=db.execute('SELECT i.*,r.full_name FROM issues i JOIN repositories r ON r.id=i.repository_id WHERE i.repository_id=? AND i.github_number=?',(repository,number)).fetchone()
        if row is None:
            raise KeyError(key)
        return row

    @staticmethod
    def _thread(db,key):
        issue=DiscussionStore._issue(db,key)
        return db.execute('SELECT * FROM discussions WHERE issue_id=?',(issue['id'],)).fetchone()

    def _event(self,db,thread,kind,actor='supervisor:discussion',**details):
        db.execute("INSERT INTO events(transaction_id,unit_id,issue_id,kind,at,actor_id,payload_json) VALUES(?,?,?,'lifecycle_changed',?,?,?)",
            (str(uuid.uuid4()),thread['unit_id'],thread['issue_id'],self.clock(),actor,_json({'discussion_id':thread['id'],'discussion_event':kind,**details})))

    def _touch(self,db,identifier):
        db.execute('UPDATE discussions SET version=version+1,updated_at=? WHERE id=?',(self.clock(),identifier))

    @staticmethod
    def _append(db,thread,entry,actor):
        previous=db.execute('SELECT * FROM messages WHERE id=?',(entry['id'],)).fetchone()
        if previous:
            if previous['discussion_id']!=thread['id'] or previous['content']!=entry['text']:
                raise IdempotencyConflict('message identity already has different content')
            return
        sequence=db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE discussion_id=?',(thread['id'],)).fetchone()[0]
        if sequence>1000 and entry['role']=='user' or sequence>1002:
            raise TransitionError('conversation has reached its retained message limit')
        metadata={k:v for k,v in entry.items() if k not in {'id','role','sequence','text','at'}}
        db.execute('INSERT INTO messages(id,discussion_id,sequence,author_id,role,content,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)',
            (entry['id'],thread['id'],sequence,actor,'operator' if entry['role']=='user' else entry['role'],entry['text'],entry['at'],_json(metadata)))

    def send(self,key,text,request_id,revision,*,actor):
        with self.store.write() as db:
            writable(db)
            issue=self._issue(db,key)
            thread=self._thread(db,key)
            if thread is None:
                identifier=str(uuid.uuid4())
                unit=db.execute('SELECT unit_id FROM unit_issues WHERE issue_id=? AND left_at IS NULL ORDER BY joined_at DESC LIMIT 1',(issue['id'],)).fetchone()
                db.execute('INSERT INTO discussions(id,repository_id,unit_id,issue_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (identifier,issue['repository_id'],unit[0] if unit else None,issue['id'],'',self.clock(),self.clock()))
                thread=self._thread(db,key)
            prior=db.execute('SELECT * FROM messages WHERE id=?',(request_id,)).fetchone()
            if prior:
                if prior['discussion_id']!=thread['id'] or prior['content']!=text or prior['author_id']!=actor:
                    raise IdempotencyConflict('message identity already belongs to another request')
                return request_id
            working=thread['status'] in {'queued','running','cancelling'}
            latest=db.execute("SELECT * FROM messages WHERE discussion_id=? AND role IN ('operator','assistant') ORDER BY sequence DESC LIMIT 1",(thread['id'],)).fetchone()
            if latest and latest['role']=='operator' and latest['content']==text:
                if working:
                    return latest['id']
                identifier=latest['id']
            elif working:
                raise TransitionError('Wait for this reply or cancel it before sending another question')
            else:
                identifier=request_id
                self._append(db,thread,dict(id=request_id,role='user',text=text,at=self.clock()),actor)
            db.execute("UPDATE discussions SET status='queued',reason='Looking for someone available',partial='',requested_at=?,source_revision=?,cancel_requested=0,presence_json=? WHERE id=?",
                (self.clock(),revision,_json({'phase':'finding','waiting_since':self.clock(),'code':'waiting','reason':'Looking for someone available'}),thread['id']))
            self._touch(db,thread['id'])
            self._event(db,thread,'queued',actor,request_id=identifier)
            return identifier

    @staticmethod
    def _artifact(db,job_id,kind):
        rows=db.execute('SELECT * FROM discussion_artifacts WHERE execution_id=? AND kind=? ORDER BY sequence LIMIT 9',(job_id,kind)).fetchall()
        if len(rows)>8 or any(row['sequence']!=index or hashlib.sha256(row['content']).hexdigest()!=row['digest'] for index,row in enumerate(rows)):
            raise TransitionError('discussion evidence is corrupt')
        return b''.join(row['content'] for row in rows)

    @staticmethod
    def _job(db,job_id):
        row=db.execute('SELECT * FROM discussion_executions WHERE id=?',(job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        runtime=db.execute('SELECT * FROM discussion_runtime WHERE execution_id=?',(job_id,)).fetchone()
        if runtime is None:
            if not row['cleanup']:
                raise TransitionError('imported discussion execution is not qualified to run')
            return json.loads(DiscussionStore._artifact(db,job_id,'launch_input'))
        value=json.loads(runtime['manifest_json'])
        value.update(fenced=bool(row['fenced']),cleanup=bool(row['cleanup']))
        for key in ('container_id','joined_at','boot','ended','excerpt'):
            if runtime[key] is not None:
                value[key]=runtime[key]
        for key in ('completed','cancelled'):
            if runtime[key]: value[key]=True
        return value

    def job(self,job_id):
        with self.store.read_snapshot() as db:
            return self._job(db,job_id)

    def thread(self,key,*,before=None,limit=1000):
        if type(limit) is not int or not 1<=limit<=1000:
            raise ValueError('discussion page limit must be bounded')
        if before is not None and (type(before) is not int or before<1):
            raise ValueError('Invalid conversation page')
        with self.store.read_snapshot() as db:
            row=self._thread(db,key)
            if row is None:
                return {'messages':[],'status':'idle','version':0}
            rows=db.execute('SELECT * FROM messages WHERE discussion_id=? '+('AND sequence<? ' if before is not None else '')+'ORDER BY sequence DESC LIMIT ?',
                (row['id'],*((before,) if before is not None else ()),limit+1)).fetchall()
            page=list(reversed(rows[:limit]))
            job_id=row['current_execution_id']
            if job_id is None:
                previous=db.execute('SELECT id FROM discussion_executions WHERE discussion_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(row['id'],)).fetchone()
                job_id=previous[0] if previous else None
            return dict(id=row['id'],version=row['version'],status=row['status'],reason=row['reason'],partial=row['partial'] or '',
                requested_at=row['requested_at'],revision=row['source_revision'],cancel_requested=bool(row['cancel_requested']),
                origin=json.loads(row['origin']) if row['origin'] else None,presence=json.loads(row['presence_json']) if row['presence_json'] else None,
                auto_retry=row['auto_retry'],avoid=json.loads(row['avoid_json']),messages=[message(m) for m in page],
                before=page[0]['sequence'] if len(rows)>limit else None,job=self._job(db,job_id) if job_id else None)

    def keys(self,*,queued=False,limit=100):
        with self.store.read_snapshot() as db:
            rows=db.execute('SELECT i.repository_id,i.github_number FROM discussions d JOIN issues i ON i.id=d.issue_id '+
                ("WHERE d.status='queued' " if queued else '')+'ORDER BY d.requested_at,d.id LIMIT ?',(limit,)).fetchall()
            return [str(r[0])+':'+str(r[1]) for r in rows]

    def active(self):
        with self.store.read_snapshot() as db:
            rows=db.execute('SELECT id FROM discussion_executions WHERE cleanup=0 ORDER BY created_at,id LIMIT 101').fetchall()
            if len(rows)>100: raise TransitionError('discussion capacity exceeded')
            return [self._job(db,row[0]) for row in rows]

    def reserve(self,key,job,*,expected_version,origin):
        if len(_json(job).encode())>262144:
            raise TransitionError('discussion assignment is too large')
        with self.store.write() as db:
            writable(db)
            thread=self._thread(db,key)
            if thread['version']!=expected_version:
                raise VersionConflict('conversation changed before launch')
            if thread['status']!='queued' or db.execute('SELECT 1 FROM discussion_leases LIMIT 1').fetchone():
                raise TransitionError('discussion is not available for reservation')
            config=job['runner_config']
            if not provider_available(db,config['provider'],job.get('provider_id'),config,self.clock()):
                raise TransitionError('coding account is still leased or unavailable')
            manifest={k:v for k,v in job.items() if k not in {'fenced','cleanup','boot'}}
            db.execute("INSERT INTO discussion_executions(id,discussion_id,phase,created_at,source_created_at) VALUES(?,?,'reserved',?,?)",(job['id'],thread['id'],self.clock(),job['created_at']))
            db.execute('INSERT INTO discussion_runtime(execution_id,manifest_json,boot) VALUES(?,?,?)',(job['id'],_json(manifest),job.get('boot')))
            db.execute('INSERT INTO discussion_leases VALUES(?,?,?,?)',(job['slot'],job['id'],job['id'],self.clock()))
            db.execute('UPDATE discussions SET current_execution_id=?,origin=?,source_revision=?,reason=? WHERE id=?',
                (job['id'],_json(origin),job['context']['revision'],'Preparing a discussion using saved issue evidence',thread['id']))
            self._touch(db,thread['id']); self._event(db,thread,'reserved',execution_id=job['id'])

    def progress(self,job_id,*,container_id=None,joined_at=None,boot=None):
        with self.store.write() as db:
            writable(db)
            job=self._job(db,job_id)
            if job['fenced'] or job['cleanup']:
                return False
            thread=db.execute('SELECT d.* FROM discussions d JOIN discussion_executions e ON e.discussion_id=d.id WHERE e.id=?',(job_id,)).fetchone()
            if thread['current_execution_id']!=job_id or thread['cancel_requested']:
                return False
            if container_id is not None:
                if job.get('container_id') and job['container_id']!=container_id:
                    raise TransitionError('discussion container identity changed')
                db.execute('UPDATE discussion_runtime SET container_id=? WHERE execution_id=?',(container_id,job_id))
            if boot is not None: db.execute('UPDATE discussion_runtime SET boot=? WHERE execution_id=?',(boot,job_id))
            if joined_at is not None:
                db.execute('UPDATE discussion_runtime SET joined_at=? WHERE execution_id=?',(joined_at,job_id))
                db.execute("UPDATE discussion_executions SET phase='running' WHERE id=?",(job_id,))
                from ..factory.discussions import agent
                from ..factory.activity import started
                here=json.loads(thread['presence_json'] or '{}')
                presence={'phase':'joined','agent':agent(job),'joined_at':joined_at,'instead_of':here.get('instead_of'),'activity':started(joined_at),'seen':[]}
                db.execute("UPDATE discussions SET status='running',reason='Agent is reading the saved evidence',presence_json=? WHERE id=?",(_json(presence),thread['id']))
            self._touch(db,thread['id']); self._event(db,thread,'progress',execution_id=job_id)
            return True

    def activity(self,key,job_id,*,partial,presence):
        with self.store.write() as db:
            writable(db)
            thread=self._thread(db,key)
            job=self._job(db,job_id)
            if thread['current_execution_id']!=job_id or job['fenced']:
                return
            db.execute('UPDATE discussions SET partial=?,presence_json=? WHERE id=?',(partial[:12000],_json(presence),thread['id']))
            self._touch(db,thread['id'])

    def waiting(self,key,*,presence,reason):
        with self.store.write() as db:
            writable(db)
            thread=self._thread(db,key)
            if thread['status']=='queued':
                db.execute('UPDATE discussions SET reason=?,presence_json=? WHERE id=?',(reason,_json(presence),thread['id']))
                self._touch(db,thread['id'])

    def cancel(self,key,*,actor):
        with self.store.write() as db:
            writable(db)
            thread=self._thread(db,key)
            if thread is None or thread['status'] not in {'queued','running','cancelling'}: return
            job_id=thread['current_execution_id']
            active=job_id and not self._job(db,job_id)['cleanup']
            if active:
                db.execute('UPDATE discussion_executions SET fenced=1 WHERE id=?',(job_id,))
                db.execute('UPDATE discussion_runtime SET cancelled=1 WHERE execution_id=?',(job_id,))
            db.execute('UPDATE discussions SET cancel_requested=1,status=?,reason=? WHERE id=?',('cancelling' if active else 'cancelled','Stopping the discussion worker' if active else 'You stopped the request',thread['id']))
            self._touch(db,thread['id']); self._event(db,thread,'cancelled',actor)
            return not active

    def _capture(self,db,job_id,capture):
        structured={k:v for k,v in capture.items() if k not in {'logs','output'}}
        for kind,raw,expires in [('result',_json(structured).encode(),None),('logs',str(capture.get('logs') or '')[-1048576:].encode()[-1048576:],self.clock()+30*86400)]:
            if len(raw)>1048576: raise TransitionError('discussion capture exceeds its size limit')
            old=db.execute('SELECT digest FROM discussion_artifacts WHERE execution_id=? AND kind=? AND sequence=0',(job_id,kind)).fetchone()
            digest=hashlib.sha256(raw).hexdigest()
            if old:
                if old[0]!=digest: raise IdempotencyConflict('discussion capture cannot be replaced')
                continue
            db.execute('INSERT INTO discussion_artifacts VALUES(?,?,0,?,?,?,?,?,?)',(job_id,kind,'application/json' if kind=='result' else 'text/plain',digest,raw,len(raw),self.clock(),expires))

    def answer(self,key,job_id,entry,*,capture,presence):
        with self.store.write() as db:
            writable(db)
            job=self._job(db,job_id); thread=self._thread(db,key)
            if job.get('completed'): return True
            if job['fenced'] or thread['current_execution_id']!=job_id or thread['cancel_requested']:
                return False
            self._capture(db,job_id,capture)
            self._append(db,thread,entry,'agent:discussion')
            db.execute('UPDATE discussion_runtime SET completed=1 WHERE execution_id=?',(job_id,))
            db.execute("UPDATE discussion_executions SET fenced=1,phase='collecting',completion_at=? WHERE id=?",(self.clock(),job_id))
            db.execute("UPDATE discussions SET partial='',status='running',reason='Finishing the discussion',presence_json=? WHERE id=?",(_json(presence),thread['id']))
            self._touch(db,thread['id']); self._event(db,thread,'answered',execution_id=job_id)
            return True

    def leave(self,key,*,expected_version,thread,job=None,capture=None):
        """Commit one classified exit and optional bounded retry, never worker authority."""
        with self.store.write() as db:
            writable(db)
            current=self._thread(db,key)
            if current['version']!=expected_version:
                raise VersionConflict('conversation changed while classifying its exit')
            if job:
                if current['current_execution_id']!=job['id']: raise TransitionError('discussion assignment is obsolete')
                actual=self._job(db,job['id'])
                if actual.get('ended') or actual.get('completed'): return
                db.execute('UPDATE discussion_executions SET fenced=1 WHERE id=?',(job['id'],))
                db.execute('UPDATE discussion_runtime SET ended=?,excerpt=? WHERE execution_id=?',(job.get('ended'),job.get('excerpt','')[:16000],job['id']))
                if capture is not None: self._capture(db,job['id'],capture)
            for entry in thread['messages'][-2:]:
                if entry['role']=='system': self._append(db,current,entry,'supervisor:discussion')
            db.execute('UPDATE discussions SET status=?,reason=?,partial=?,presence_json=?,auto_retry=?,avoid_json=?,requested_at=?,cancel_requested=? WHERE id=?',
                (thread['status'],thread.get('reason'),thread.get('partial',''),_json(thread.get('presence')),thread.get('auto_retry'),_json(thread.get('avoid',{})),thread.get('requested_at'),int(thread.get('cancel_requested',False)),current['id']))
            self._touch(db,current['id']); self._event(db,current,'left',reason=job.get('ended') if job else thread['status'])

    def cleanup(self,job_id):
        """Caller has independently verified process/resource teardown."""
        with self.store.write() as db:
            writable(db)
            job=self._job(db,job_id)
            if not job['fenced']: raise TransitionError('discussion must be fenced before cleanup')
            if job['cleanup']: return
            thread=db.execute('SELECT d.* FROM discussions d JOIN discussion_executions e ON e.discussion_id=d.id WHERE e.id=?',(job_id,)).fetchone()
            db.execute("UPDATE discussion_executions SET cleanup=1,phase='cleaned' WHERE id=?",(job_id,))
            db.execute('DELETE FROM discussion_leases WHERE execution_id=?',(job_id,))
            if thread['current_execution_id']==job_id:
                status='idle' if job.get('completed') else 'cancelled' if thread['status']=='cancelling' else 'unavailable' if thread['status']=='running' else thread['status']
                db.execute('UPDATE discussions SET status=?,reason=CASE WHEN ? THEN NULL ELSE reason END WHERE id=?',(status,int(bool(job.get('completed'))),thread['id']))
                self._touch(db,thread['id'])
            self._event(db,thread,'cleaned',execution_id=job_id)

    def receipt(self,key,message_id,receipt,*,actor,**details):
        with self.store.write() as db:
            writable(db)
            thread=self._thread(db,key)
            row=db.execute("SELECT * FROM messages WHERE discussion_id=? AND id=? AND role='assistant'",(thread['id'],message_id)).fetchone()
            if row is None: raise KeyError(message_id)
            metadata=json.loads(row['metadata_json'])
            if metadata.get('receipt') and metadata['receipt']!=receipt:
                raise IdempotencyConflict('proposal already has another receipt')
            metadata['receipt']=receipt
            db.execute('UPDATE messages SET metadata_json=? WHERE id=?',(_json(metadata),message_id))
            self._event(db,thread,'proposal_applied',actor,message_id=message_id,receipt=receipt,**details)

    def mark(self,key,message_id,*,actor,reason=None,**fields):
        """Record what became of an answer's proposal: a failed close or its undo."""
        with self.store.write() as db:
            writable(db)
            thread=self._thread(db,key)
            row=db.execute("SELECT * FROM messages WHERE discussion_id=? AND id=? AND role='assistant'",(thread['id'],message_id)).fetchone()
            if row is None: raise KeyError(message_id)
            metadata=json.loads(row['metadata_json'])|fields
            db.execute('UPDATE messages SET metadata_json=? WHERE id=?',(_json(metadata),message_id))
            self._touch(db,thread['id'])
            self._event(db,thread,'proposal_'+next(iter(fields)),actor,message_id=message_id,**({'reason':reason} if reason else {}))

    def asker(self,key):
        """(author, words) of the operator's latest message."""
        with self.store.read_snapshot() as db:
            thread=self._thread(db,key)
            row=db.execute("SELECT author_id,content FROM messages WHERE discussion_id=? AND role='operator' ORDER BY sequence DESC LIMIT 1",(thread['id'],)).fetchone()
            return (row['author_id'],row['content']) if row else None

    def prune_logs(self):
        with self.store.write() as db:
            writable(db)
            db.execute("DELETE FROM discussion_artifacts WHERE rowid IN (SELECT a.rowid FROM discussion_artifacts a JOIN discussion_executions e ON e.id=a.execution_id WHERE a.kind='logs' AND a.expires_at<=? AND e.cleanup=1 LIMIT 100)",(self.clock(),))

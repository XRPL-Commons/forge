"""Explicit scoped CLI authority for repository merge policy."""
from copy import deepcopy
import json
import re

from .client_connections import fields, need
from .factory.config import load, validate, merge_mode, policy
from .settings import digest
from .store.configuration import Configuration
from .store.runtime import writable


def describe(config, repository_id, repository_name=None):
    return {'mode':merge_mode(config,repository_id),
            'source':'explicit' if str(repository_id) in config['repository_merge_policies'] else 'default',
            'revision':digest(config),
            'policy_hash':digest(policy(config,repository_id,repository_name=repository_name))}


class ClientMergePolicy:
    def __init__(self, settings, clients):
        self.settings, self.clients = settings, clients
        self.store = settings.authority.database
        need(self.store is not None,'setup_required','Select the SQLite runtime before changing repository merge policy.',409)

    def request(self, token, value):
        need(isinstance(value,dict))
        action=value.get('action')
        need(action in {'read','set'})
        common={'repository','instance','generation','action'}
        fields(value,common if action=='read' else common|{'mode','revision','request_id'})
        capability='repositories:read' if action=='read' else 'repositories:policy'
        grant=self.clients.authorize(token,value['repository'],capability,value['instance'],value['generation'])
        if action=='read':
            config=load(self.settings.authority.root,scope=self.settings.scope)
            with self.store.read_snapshot() as db:
                self._repository(db,value['repository'])
            answer={'repository':value['repository'],**describe(config,value['repository']['id'],value['repository']['name'])}
            self.clients.authorize(token,value['repository'],capability,value['instance'],value['generation'])
            return answer
        need(isinstance(value['mode'],str) and value['mode'] in {'auto','human'})
        need(isinstance(value['revision'],str) and re.fullmatch(r'[a-f0-9]{64}',value['revision']))
        need(isinstance(value['request_id'],str) and re.fullmatch(r'[a-f0-9]{32}',value['request_id']))
        binding={**deepcopy(value),'client':grant['id'],'operator':grant['operator']}
        from .mutation_audit import current, MutationAudit, request_context
        audit=current() or MutationAudit(self.store,'/api/client/policy',clock=self.settings.clock,request_id=value['request_id'])
        audit.bind(grant=grant); audit.describe(value)
        with request_context(audit),self.clients.effect_guard(token,value['instance'],value['generation']),self.store.write() as db:
            writable(db)
            self._repository(db,value['repository'])
            row=db.execute('SELECT payload_json,result_json FROM client_operations WHERE id=?',(value['request_id'],)).fetchone()
            if row:
                need(json.loads(row['payload_json'])==binding,'operation_conflict','This request ID belongs to another policy decision.',409)
                return json.loads(row['result_json'])
            domain=self.settings.authority.domain('runtime.json')
            stored=db.execute("SELECT * FROM configuration WHERE domain=? AND subject='instance' AND active=1",(domain,)).fetchone()
            config=validate(json.loads(stored['settings_json']) if stored else {})
            need(digest(config)==value['revision'],'revision_changed','Runtime settings changed. Read the current policy before setting it.',409)
            updated=deepcopy(config)
            updated['repository_merge_policies'][str(value['repository']['id'])]=value['mode']
            if updated != config:
                Configuration(self.store,clock=self.settings.clock).replace_in(db,domain,updated,
                    expected_revision=stored['revision'] if stored else 0,credential_ref=stored['credential_ref'] if stored else None)
            answer={'repository':value['repository'],**describe(updated,value['repository']['id'],value['repository']['name'])}
            db.execute("INSERT INTO client_operations(id,request_id,connection_id,repository_id,kind,status,payload_json,result_json,created_at,updated_at,finished_at) "
                "VALUES(?,?,?,?,'merge_policy','completed',?,?,?,?,?)",(value['request_id'],value['request_id'],grant['id'],value['repository']['id'],
                json.dumps(binding,sort_keys=True),json.dumps(answer,sort_keys=True),self.settings.clock(),self.settings.clock(),self.settings.clock()))
            audit.details['command_request_id']=value['request_id']
            audit.finish_in(db,'accepted','policy_saved')
            return answer

    def _repository(self,db,target):
        domain=self.settings.authority.domain('factory.json')
        row=db.execute("SELECT settings_json FROM configuration WHERE domain=? AND subject='instance' AND active=1",(domain,)).fetchone()
        repositories=json.loads(row[0]).get('repositories',[]) if row else []
        need(any(repo['id']==target['id'] and repo['name'].lower()==target['name'].lower() for repo in repositories),
             'repository_not_configured','Configure this repository before setting its merge policy.',409)
        # The receipt references this immutable repository identity, already
        # admitted by canonical Settings. A rename cannot redirect its authority.
        row=db.execute('SELECT full_name FROM repositories WHERE id=?',(target['id'],)).fetchone()
        need(row is not None and row[0].lower()==target['name'].lower(),'scope_changed','Repository identity changed.',409)

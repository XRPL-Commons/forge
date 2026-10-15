"""Approved compositions and one logical builder's tracked execution tree.

Every operation shares the existing unit version, decision journal, failure budget
and execution leases. This module performs no provider, filesystem or GitHub I/O.
"""
from dataclasses import asdict
import json

from .models import Candidate, Command, TransitionError, VersionConflict, complete_requirements
from .transitions import Lifecycle, _id, _json, _unit, _gates, _charges, applicable_gates, manifest_hash, project_unit
from .delivery_inputs import approved_in, members_in, dependencies_in


from .task_commands import TaskCommands


class Delivery(TaskCommands, Lifecycle):
    @staticmethod
    def composition_hash(*, repository_id, base_branch, members, dependencies=(), existing_prs=(), rationale, publication_target=None):
        return manifest_hash(dict(repository_id=repository_id, base_branch=base_branch,
            members=sorted(members, key=lambda item: item['issue_id']),
            dependencies=sorted(dependencies, key=lambda item: (item['issue_id'], item['prerequisite_issue_id'])),
            existing_prs=sorted(existing_prs, key=lambda item: item['unit_id']), rationale=rationale,
            publication_target=publication_target if publication_target is not None else {'kind':'new'}))

    def compose(self, command, *, repository_id, base_branch, members, rationale, approval,
                dependencies=(), existing_prs=(), publication_target=None):
        proposal = dict(repository_id=repository_id,base_branch=base_branch,members=members,rationale=rationale,
                        approval=approval,dependencies=dependencies,existing_prs=existing_prs,publication_target=publication_target)
        with self._command_transaction(command, 'compose', proposal) as db:
            return self.compose_in(db, command, **proposal)

    def compose_in(self, db, command, *, repository_id, base_branch, members, dependencies=(), existing_prs=(), rationale, approval, publication_target=None):
        payload = dict(repository_id=repository_id, base_branch=base_branch, members=members,
                       dependencies=dependencies, existing_prs=existing_prs, rationale=rationale, approval=approval, publication_target=publication_target)
        previous = self._replay(db, command, 'compose', payload)
        if previous is not None:
            return previous
        if command.expected_version != 1 or db.execute('SELECT 1 FROM units WHERE id=?', (command.unit_id,)).fetchone():
            raise VersionConflict('composition requires a new unit identity')
        if (not command.actor_id or not command.request_id or not isinstance(rationale, str) or not rationale.strip()
                or not isinstance(members, (list, tuple)) or len(members) < 2):
            raise TransitionError('composition requires explicit rationale and at least two pinned issues')
        if approval != {'approved': True, 'composition_hash': self.composition_hash(repository_id=repository_id,
                base_branch=base_branch, members=members, dependencies=dependencies, existing_prs=existing_prs, rationale=rationale, publication_target=publication_target)}:
            raise TransitionError('operator approval must pin the exact composition')
        repository = db.execute('SELECT enabled FROM repositories WHERE id=?', (repository_id,)).fetchone()
        if not repository or not repository[0] or not isinstance(base_branch, str) or not base_branch:
            raise TransitionError('composition requires an enabled repository and base branch')
        pins, sources, issues = {}, {}, {}
        for pin in members:
            if set(pin) != {'issue_id','issue_version','requirements_revision_id','unit_id','unit_version','member_id'} or pin['issue_id'] in pins:
                raise TransitionError('composition members require unique complete identity pins')
            issue = db.execute('SELECT * FROM issues WHERE id=?', (pin['issue_id'],)).fetchone()
            if (issue is None or issue['repository_id'] != repository_id or issue['github_state'] != 'open'
                    or issue['version'] != pin['issue_version'] or issue['current_revision_id'] != pin['requirements_revision_id']):
                raise VersionConflict('issue identity or requirements changed before composition')
            kinds = json.loads(issue['assignee_types_json'])
            if any(kinds.get(login) != 'Bot' for login in json.loads(issue['assignees_json'])) or set(json.loads(issue['labels_json'])) & {'hold','blocked','wip'}:
                raise TransitionError('held or human-owned issues cannot be composed')
            requirements = json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?', (pin['requirements_revision_id'],)).fetchone()[0])
            if not complete_requirements(requirements):
                raise TransitionError('composition requires complete requirements')
            membership = db.execute('SELECT * FROM unit_issues WHERE issue_id=? AND left_at IS NULL', (pin['issue_id'],)).fetchone()
            if membership is None:
                if any(pin[key] is not None for key in ('unit_id','unit_version','member_id')):
                    raise VersionConflict('issue membership changed before composition')
            else:
                source = _unit(db, membership['unit_id'])
                self._live(source)
                if (pin['unit_id'],pin['unit_version'],pin['member_id']) != (source['id'],source['version'],membership['id']) or source['base_branch'] != base_branch:
                    raise VersionConflict('source delivery changed before composition')
                self._quiescent(db, source['id'])
                if any(gate['code'] not in {'dependency','verification_human','verification_ci','approval_required','budget_exhausted'}
                       for gate in applicable_gates(db, source, membership['id'], source['current_candidate_id'])):
                    raise TransitionError('source delivery has an unresolved ownership or lifecycle gate')
                sources[source['id']] = source
            pins[pin['issue_id']], issues[pin['issue_id']] = pin, dict(issue)
        for source in sources.values():
            if any(member['issue_id'] not in pins for member in self._members(db, source['id'])):
                raise TransitionError('composition must include every live member of each source delivery')
        receipts = {receipt['unit_id']: receipt for receipt in existing_prs}
        if len(receipts) != len(existing_prs) or set(receipts) != {key for key, source in sources.items() if source['current_candidate_id']}:
            raise TransitionError('every existing PR needs its own explicit verified composition choice')
        for source_id, receipt in receipts.items():
            author = receipt.get('author')
            if (not isinstance(author,dict) or set(author) != {'id','login','type'}
                    or type(author['id']) is not int or author['id'] <= 0 or author['type'] not in {'User','Bot'}
                    or not isinstance(author['login'],str) or not author['login'].strip()):
                raise TransitionError('every approved source PR needs its exact author identity')
            self._prove_not_merged(db, sources[source_id], receipt)
        if publication_target is None:
            if receipts:
                raise TransitionError('existing PR composition requires an explicit publication target')
            publication_target = {'kind':'new'}
        if not isinstance(publication_target,dict):
            raise TransitionError('publication target must select a new or exact existing PR')
        if publication_target.get('kind') == 'new':
            valid_target = set(publication_target) == {'kind'}
        elif publication_target.get('kind') == 'existing':
            receipt = receipts.get(publication_target.get('unit_id'))
            author = publication_target.get('author')
            valid_target = bool(receipt and set(publication_target) == {'kind','unit_id','candidate_id','pull_number','head_oid','author','ownership_transfer'}
                and publication_target['ownership_transfer'] is True
                and isinstance(author,dict) and set(author) == {'id','type','login'}
                and type(author['id']) is int and author['id'] > 0 and author['type'] in {'User','Bot'}
                and isinstance(author['login'],str) and author['login'].strip()
                and all(publication_target[key] == receipt.get(key) for key in ('candidate_id','pull_number','head_oid','author')))
        else:
            valid_target = False
        if not valid_target:
            raise TransitionError('publication target must match one explicitly approved existing head')
        graph = {issue_id: set() for issue_id in pins}
        for edge in dependencies:
            if set(edge) != {'issue_id','prerequisite_issue_id'} or edge['issue_id'] not in graph or edge['prerequisite_issue_id'] not in graph:
                raise TransitionError('internal dependencies must identify approved members')
            graph[edge['issue_id']].add(edge['prerequisite_issue_id'])
        for issue_id, pin in pins.items():
            for dependency in db.execute('SELECT d.prerequisite_issue_id,i.github_state,i.state_reason FROM issue_dependencies d '
                    'JOIN issues i ON i.id=d.prerequisite_issue_id WHERE d.issue_id=? AND d.requirements_revision_id=?',
                    (issue_id,pin['requirements_revision_id'])):
                if dependency[0] in pins:
                    if dependency[0] not in graph[issue_id]:
                        raise TransitionError('composition omitted a declared internal dependency')
                elif dependency['github_state'] != 'closed' or dependency['state_reason'] != 'completed':
                    raise TransitionError('an external prerequisite is not completed')
        visited = set()
        def visit(member, path=()):
            if member in path:
                raise TransitionError('composition dependency graph contains a cycle')
            if member not in visited:
                for predecessor in graph[member]:
                    visit(predecessor, (*path, member))
                visited.add(member)
        for issue_id in graph:
            visit(issue_id)
        inherited = set()
        for source in sources.values():
            inherited.update(row[0] for row in db.execute('SELECT id FROM attempts WHERE unit_id=? AND budget_generation=?', (source['id'],source['budget_generation'])))
            inherited.update(row[0] for row in db.execute('SELECT attempt_id FROM unit_budget_lineage WHERE unit_id=? AND budget_generation=?', (source['id'],source['budget_generation'])))
            child = Command(command.request_id, command.actor_id, source['id'], source['version'])
            for gate in _gates(db, source['id']):
                self._resolve(db, child, gate['id'], {'combined_into': command.unit_id}, resolution='superseded')
            self._fence(db, child)
            for member in self._members(db, source['id']):
                db.execute("UPDATE unit_issues SET left_at=?,exit_reason='combined',state='removed' WHERE id=?", (self.clock(),member['id']))
                self._event(db,child,'membership_changed',{'member_id':member['id'],'combined_into':command.unit_id},issue_id=member['issue_id'])
            db.execute("UPDATE units SET state='closed',close_code='combined',primary_gate_id=NULL,version=version+1,updated_at=? WHERE id=?", (self.clock(),source['id']))
            self._event(db,child,'closed',{'reason':'combined','target_unit_id':command.unit_id})
        db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,?,'building',?,?)", (command.unit_id,repository_id,base_branch,self.clock(),self.clock()))
        db.execute('INSERT INTO unit_publication_targets VALUES(?,?)',(command.unit_id,_json(publication_target)))
        for source in sources.values():
            db.execute('INSERT INTO unit_composition_sources VALUES(?,?,?)',(command.unit_id,source['id'],source['current_candidate_id']))
        result_members = []
        for issue_id,pin in sorted(pins.items()):
            member_id = _id(command.request_id,'member:'+issue_id)
            db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES(?,?,?,?,?,?,'pending',?)", (member_id,command.unit_id,issue_id,repository_id,base_branch,pin['requirements_revision_id'],self.clock()))
            result_members.append({'member_id':member_id,'issue_id':issue_id,'requirements_revision_id':pin['requirements_revision_id']})
            self._event(db,command,'membership_changed',{'member_id':member_id,'source_unit_id':pin['unit_id'],'source_member_id':pin['member_id']},issue_id=issue_id)
        mapped = {member['issue_id']:member['member_id'] for member in result_members}
        for issue_id, predecessors in graph.items():
            for predecessor in predecessors:
                db.execute('INSERT INTO member_dependencies VALUES(?,?,?,1)', (command.unit_id,mapped[issue_id],mapped[predecessor]))
        for attempt_id in inherited:
            db.execute('INSERT INTO unit_budget_lineage VALUES(?,1,?)', (command.unit_id,attempt_id))
        if _charges(db,_unit(db,command.unit_id)) >= 3:
            self._open_gate(db,command,'budget_exhausted',{'limit':3,'source':'composition'})
        self._event(db,command,'unit_created',{'composition_hash':approval['composition_hash'],'source_units':sorted(sources)})
        return self._save(db,command,'compose',payload,{'members':result_members},initial=True)

    @staticmethod
    def _builder_manifest_in(db, unit, stage, base_oid, policy_hash):
        from .task_inputs import builder_manifest_in
        return builder_manifest_in(db,unit,stage,base_oid,policy_hash)

    def _reserve_in(self, db, command, unit, *, slot, execution_fence, input_manifest, member_id=None, launch=None, execution_kind="legacy"):
        if not isinstance(slot,str) or not slot or not isinstance(execution_fence,str) or not execution_fence:
            raise TransitionError('assignment requires a capacity slot and execution fence')
        if _charges(db,unit) >= 3:
            raise TransitionError('unit failure budget is exhausted')
        attempt_id,job_id = _id(command.request_id,'attempt'),_id(command.request_id,'job')
        db.execute('INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (attempt_id,unit['id'],member_id,unit['membership_generation'],unit['fencing_generation'],_json(input_manifest),unit['budget_generation'],self.clock()))
        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,created_at,execution_kind) VALUES(?,?,?,?,'build','reserved',?,?)", (job_id,unit['id'],attempt_id,member_id,self.clock(),execution_kind))
        db.execute('INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)', (slot,job_id,execution_fence,self.clock()))
        self._event(db,command,'attempt_reserved',{'input_manifest':input_manifest,'member_id':member_id},job_id=job_id)
        return {'job_id':job_id,'attempt_id':attempt_id}

    def reserve_builder(self, command, *, stage, slot, execution_fence, input_manifest, launch=None):
        payload = dict(stage=stage,slot=slot,execution_fence=execution_fence,input_manifest=input_manifest,
                       launch=launch.payload() if launch else None)
        def change(db,unit):
            self._live(unit)
            if not approved_in(db,unit['id']) or not members_in(db,unit['id']) or unit['state'] != 'building':
                raise TransitionError('owning builder reservation requires a grouped delivery in code work')
            self._quiescent(db,unit['id'])
            expected = self._builder_manifest_in(db,unit,stage,input_manifest.get('base_oid'),input_manifest.get('policy_hash'))
            if input_manifest != expected:
                raise TransitionError('builder manifest differs from current approved membership or outputs')
            if any(gate['member_id'] is None for gate in applicable_gates(db,unit,candidate_id=unit['current_candidate_id'])):
                raise TransitionError('an applicable unit gate blocks the owning builder')
            from .task_inputs import scoped_task_gates_in
            if stage == 'integration' and scoped_task_gates_in(db,unit,[member['id'] for member in members_in(db,unit['id'])],candidate_id=unit['current_candidate_id']):
                raise TransitionError('a member gate blocks combined integration')
            builder = db.execute('SELECT id FROM unit_builders WHERE unit_id=?',(unit['id'],)).fetchone()
            builder_id = builder[0] if builder else _id(command.request_id,'builder')
            if builder is None:
                db.execute('INSERT INTO unit_builders VALUES(?,?,?,?)',(builder_id,unit['id'],command.actor_id,self.clock()))
            job = self._reserve_in(db,command,unit,slot=slot,execution_fence=execution_fence,input_manifest=input_manifest,launch=launch)
            db.execute('INSERT INTO builder_assignments VALUES(?,?,?)',(job['job_id'],builder_id,stage))
            if launch is not None:
                from .executions import insert_launch
                insert_launch(db,job['job_id'],slot,execution_fence,launch.payload(),self.clock())
            target = 'building'
            db.execute('UPDATE units SET state=? WHERE id=?',(target,unit['id']))
            self._event(db,command,'phase_changed',{'before':unit['state'],'after':target,'builder_id':builder_id},job_id=job['job_id'])
            return {**job,'builder_id':builder_id}
        return self._apply(command,'reserve_builder',payload,change)

    @staticmethod
    def _publication_target_in(manifest, candidate):
        target = manifest['publication_target']
        if manifest.get('previous_publication'):
            previous = manifest['previous_publication']
            valid = candidate.pull_number == previous['pull_number'] and candidate.branch == previous['branch']
        elif target['kind'] == 'existing':
            source = next(item for item in manifest['existing_candidates'] if item['candidate_id'] == target['candidate_id'])
            valid = candidate.pull_number == target['pull_number'] and candidate.branch == source['branch']
        else:
            valid = all(candidate.pull_number != item['pull_number'] and candidate.branch != item['branch']
                        for item in manifest['existing_candidates'])
        if not valid:
            raise TransitionError('candidate differs from the operator-approved publication target')

    def publish_candidate(self,command,job_id,*,execution_fence,input_manifest,candidate,evidence,gates=()):
        if not isinstance(candidate,Candidate):
            raise TransitionError('publication requires a Candidate identity')
        if any(set(gate) != {'code','details'} or gate['code'] not in {'verification_human','verification_ci','approval_required'} for gate in gates):
            raise TransitionError('publication gates must bind candidate verification or approval')
        payload = dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,candidate=asdict(candidate),evidence=evidence,gates=gates)
        def change(db,unit):
            job,current = self._job(db,unit,job_id,execution_fence,input_manifest)
            owner = db.execute("SELECT * FROM builder_assignments WHERE job_id=? AND stage='integration'",(job_id,)).fetchone()
            if owner is None:
                raise TransitionError('only the owning builder integration can publish the combined candidate')
            if not current:
                self._result(db,command,job,evidence,'stale_success')
                return {'accepted':False}
            expected = self._builder_manifest_in(db,unit,'integration',input_manifest['base_oid'],input_manifest['policy_hash'])
            if input_manifest != expected or unit['state'] not in {'building','publishing'}:
                raise TransitionError('combined candidate inputs are no longer current')
            if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND id!=? AND cleanup=0',(unit['id'],job_id)).fetchone():
                raise TransitionError('all children must be cleaned before combined publication')
            if any(g['code'] not in {'result_pending','cleanup_pending'} or json.loads(g['details_json']).get('job_id') != job_id
                   for g in self._integration_gates(db,unit,job_id)):
                raise TransitionError('a gate blocks combined publication')
            if (candidate.requirements_hash != expected['requirements_hash'] or candidate.input_manifest_hash != manifest_hash(expected)
                    or candidate.base_oid != expected['base_oid'] or candidate.policy_hash != expected['policy_hash']
                    or not isinstance(evidence,dict) or evidence.get('verified') is not True
                    or any(evidence.get(key) != getattr(candidate,key) for key in ('pull_number','head_oid','base_oid'))):
                raise TransitionError('publication must independently verify the exact integrated candidate')
            if expected.get('verification_requests') and not any(
                    gate['code'] in {'verification_human','verification_ci'}
                    and gate['details'].get('verification_job_id') == job_id for gate in gates):
                raise TransitionError('integrated candidate must retain task verification requirements')
            self._publication_target_in(expected,candidate)
            from .publications import require_receipt_in
            require_receipt_in(db,job,candidate,evidence)
            candidate_id = _id(command.request_id,'candidate')
            db.execute('INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (candidate_id,unit['id'],candidate.pull_number,candidate.branch,candidate.head_oid,candidate.base_oid,candidate.policy_hash,candidate.requirements_hash,unit['membership_generation'],candidate.input_manifest_hash,self.clock()))
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,?,'publication','pass',?,?,?)",(_id(command.request_id,'evidence'),unit['id'],candidate_id,job['attempt_id'],command.actor_id,self.clock(),_json(evidence)))
            self._result(db,command,job,evidence,'success')
            db.execute("UPDATE units SET state='reviewing',current_candidate_id=? WHERE id=?",(candidate_id,unit['id']))
            for gate in gates:
                self._open_gate(db,command,gate['code'],gate['details'],candidate_id=candidate_id)
            self._event(db,command,'candidate_selected',{'candidate_id':candidate_id,'builder_id':owner['builder_id']},job_id=job_id)
            return {'accepted':True,'candidate_id':candidate_id,'phase':'reviewing'}
        return self._apply(command,'publish_candidate',payload,change)

    @staticmethod
    def _integration_gates(db,unit,job_id):
        from .task_inputs import scoped_task_gates_in
        return scoped_task_gates_in(db,unit,[m['id'] for m in members_in(db,unit['id'])],job_id=job_id,candidate_id=unit['current_candidate_id'])

    def cancel_builder(self,command,*,evidence):
        if not isinstance(evidence,dict) or not evidence:
            raise TransitionError('cancelling a builder requires a recorded reason')
        def change(db,unit):
            self._live(unit)
            self._fence(db,command)
            return {'charged':False,'cleanup_required':True}
        return self._apply(command,'cancel_builder',{'evidence':evidence},change)

    def drop_member(self,command,member_id,*,membership_generation,dependent_decisions,evidence,disposition='release'):
        """An explicit membership decision preserves history and shared charges."""
        payload=dict(member_id=member_id,membership_generation=membership_generation,
                     dependent_decisions=dependent_decisions,evidence=evidence,disposition=disposition)
        return self._apply(command,'drop_member',payload,lambda db,unit:self.drop_member_in(db,command,unit,**payload))

    def drop_member_in(self,db,command,unit,*,member_id,membership_generation,dependent_decisions,evidence,disposition='release'):
        if not approved_in(db,unit['id']):
            raise TransitionError('member removal requires an approved composition')
        if disposition not in {'release','close'}:
            raise ValueError('A member must be released or explicitly closed')
        self._live(unit)
        self._quiescent(db,unit['id'])
        from .recovery import pending_mandatory
        if pending_mandatory(db,unit['id']):
            raise TransitionError('mandatory investigations must complete before dropping a member')
        if not isinstance(evidence,dict) or evidence.get('approved') is not True:
            raise TransitionError('dropping a member requires explicit operator approval')
        if membership_generation != unit['membership_generation']:
            raise VersionConflict('membership changed before drop')
        self._prove_not_merged(db,unit,evidence)
        members=members_in(db,unit['id'])
        removed=next((member for member in members if member['id']==member_id),None)
        if removed is None or (len(members)<2 and disposition!='close'):
            raise TransitionError('drop requires a live member of a grouped delivery')
        direct={member['id'] for member in members if member_id in dependencies_in(db,unit,member['id'])}
        if dependent_decisions != {member:'wait_external' for member in direct}:
            raise TransitionError('each dependent must explicitly wait for dropped work as an external prerequisite')
        affected={member_id}
        while True:
            descendants={member['id'] for member in members if set(dependencies_in(db,unit,member['id'])) & affected}
            if descendants.issubset(affected): break
            affected.update(descendants)
        edges=[(member['id'],predecessor) for member in members for predecessor in dependencies_in(db,unit,member['id'])
               if member['id']!=member_id and predecessor!=member_id]
        issue=dict(db.execute('SELECT * FROM issues WHERE id=?',(removed['issue_id'],)).fetchone())
        if disposition=='close':
            if issue['github_state']!='closed' or issue['state_reason'] not in {'completed','not_planned'}:
                raise TransitionError('member closure requires a confirmed GitHub closure')
        elif issue['github_state']!='open':
            raise TransitionError('closed members require an explicit closure decision')
        retained_intent=[gate for gate in _gates(db,unit['id']) if gate['code'] in {'operator_pause','operator_backlog'} and gate['member_id'] in {None,member_id}]
        from .forge_problems import member_faults_in, inherit_faults_in
        retained_faults = member_faults_in(db,unit['id'],member_id)
        from .worker_claims import member_claims_in, inherit_claims_in
        retained_claims = member_claims_in(db, unit['id'], member_id, self.clock())
        if len(members)==1:
            self._settle(db,command,unit,'closed',close_code='operator_closed')
            return {'released':None,'closed_issue_id':removed['issue_id'],'affected_member_ids':[member_id],'membership_generation':unit['membership_generation']}
        old_candidate=unit['current_candidate_id']
        self._fence(db,command)
        for gate in _gates(db,unit['id']):
            if gate['member_id']==member_id or gate['candidate_id'] is not None:
                self._resolve(db,command,gate['id'],{'dropped_member_id':member_id},resolution='superseded')
        from .task_inputs import affected_tasks_in
        for task_id in affected_tasks_in(db,unit['id'],affected):
            db.execute('INSERT OR IGNORE INTO part_output_invalidations(output_id,reason,evidence_json,created_at) '
                "SELECT id,'member_dropped',?,? FROM part_outputs WHERE task_id=?",(_json({'member_id':member_id}),self.clock(),task_id))
        for affected_id in affected:
            db.execute("UPDATE unit_issues SET state='pending' WHERE id=?",(affected_id,))
        for dependent in direct:
            member=next(member for member in members if member['id']==dependent)
            db.execute("INSERT OR IGNORE INTO issue_dependencies(id,issue_id,requirements_revision_id,prerequisite_issue_id,source,evidence,created_at) "
                "VALUES(?,?,?,?,'operator',?,?)",(_id(command.request_id,'external:'+dependent),member['issue_id'],member['requirements_revision_id'],removed['issue_id'],'Explicit dependency retained after member drop',self.clock()))
        db.execute("UPDATE unit_issues SET left_at=?,exit_reason='dropped',state='removed' WHERE id=?",(self.clock(),member_id))
        db.execute("UPDATE units SET membership_generation=membership_generation+1,current_candidate_id=NULL,state='building' WHERE id=?",(unit['id'],))
        for member,predecessor in edges:
            db.execute('INSERT INTO member_dependencies VALUES(?,?,?,?)',(unit['id'],member,predecessor,unit['membership_generation']+1))
        released_id=None;released=None
        if disposition=='release':
            released_id=_id(command.request_id,'released-unit')
            released_member=self._create_unit(db,command,released_id,issue,unit['base_branch'],removed['requirements_revision_id'])
            inherited={row[0] for row in db.execute('SELECT id FROM attempts WHERE unit_id=? AND budget_generation=?',(unit['id'],unit['budget_generation']))}
            inherited.update(row[0] for row in db.execute('SELECT attempt_id FROM unit_budget_lineage WHERE unit_id=? AND budget_generation=?',(unit['id'],unit['budget_generation'])))
            for attempt in inherited: db.execute('INSERT INTO unit_budget_lineage VALUES(?,1,?)',(released_id,attempt))
            released_command=Command(command.request_id,command.actor_id,released_id,1)
            for code in sorted({gate['code'] for gate in retained_intent}):
                self._open_gate(db,released_command,code,{'source_unit_id':unit['id'],'source_gate_ids':[gate['id'] for gate in retained_intent if gate['code']==code]})
            inherit_faults_in(self,db,released_command,released_member,retained_faults)
            inherit_claims_in(self, db, released_command, released_member, retained_claims)
            # Moving failed work is not permission to reset its original shared budget.
            if _charges(db,_unit(db,released_id))>=3:
                self._open_gate(db,released_command,'budget_exhausted',{'limit':3,'source':'member_drop'})
            released={'unit_id':released_id,'member_id':released_member,'issue_id':removed['issue_id']}
        for gate in db.execute("SELECT id FROM unit_gates g WHERE unit_id=? AND code='forge_problem' AND resolved_at IS NULL "
                              'AND EXISTS(SELECT 1 FROM forge_problem_jobs f WHERE f.gate_id=g.id)',(unit['id'],)).fetchall():
            if not db.execute('SELECT 1 FROM forge_problem_jobs f JOIN unit_issues m ON m.id=f.member_id WHERE f.gate_id=? AND m.left_at IS NULL LIMIT 1',(gate['id'],)).fetchone():
                self._resolve(db,command,gate['id'],{'removed_member_id':member_id},resolution='superseded')
        for gate in db.execute("SELECT id FROM unit_gates g WHERE unit_id=? AND code='environment' AND resolved_at IS NULL "
                              'AND EXISTS(SELECT 1 FROM worker_claim_obligations o WHERE o.gate_id=g.id)', (unit['id'],)).fetchall():
            if not db.execute('SELECT 1 FROM worker_claim_obligations o JOIN unit_issues m ON m.id=o.member_id '
                              'WHERE o.gate_id=? AND o.released_at IS NULL AND m.left_at IS NULL LIMIT 1',
                              (gate['id'],)).fetchone():
                self._resolve(db, command, gate['id'], {'removed_member_id': member_id}, resolution='superseded')
        if old_candidate:
            self._event(db,command,'candidate_superseded',{'candidate_id':old_candidate,'reason':'member_dropped'})
        self._event(db,command,'membership_changed',{'member_id':member_id,'released_unit_id':released_id,'affected_member_ids':sorted(affected)},issue_id=removed['issue_id'])
        return {'released':released,'closed_issue_id':removed['issue_id'] if disposition=='close' else None,
                'affected_member_ids':sorted(affected),'membership_generation':unit['membership_generation']+1}


class DeliveryQueries:
    def __init__(self,store):
        self.store = store

    def assignments(self,unit_id,*,base_oid,policy_hash,limit=100,allowed_members=None):
        from .queries import _limit
        from .task_queries import assignments_in
        limit=_limit(limit)
        if allowed_members is not None:
            if not isinstance(allowed_members,(set,frozenset,list,tuple)) or any(not isinstance(x,str) for x in allowed_members):
                raise ValueError('Allowed members must be explicit member IDs')
            allowed_members=set(allowed_members)
        with self.store.read_snapshot() as db:
            return assignments_in(db,_unit(db,unit_id),base_oid=base_oid,policy_hash=policy_hash,limit=limit,allowed_members=allowed_members)

    def tree(self,unit_id,*,after='',limit=100,task_after='',task_limit=50):
        from .queries import _limit
        limit = _limit(limit)
        if not isinstance(after,str):
            raise ValueError('execution tree cursor must be a job ID')
        task_limit=_limit(task_limit)
        with self.store.read_snapshot() as db:
            unit = project_unit(db,unit_id)
            builder = db.execute('SELECT * FROM unit_builders WHERE unit_id=?',(unit_id,)).fetchone()
            jobs = [dict(row) for row in db.execute('SELECT j.id,j.attempt_id,j.member_id,j.role,j.phase,j.outcome,j.fenced,j.cleanup,'
                'a.stage,d.plan_id,d.delegation_key,d.worktree_key,p.parent_job_id,l.slot '
                'FROM jobs j LEFT JOIN builder_assignments a ON a.job_id=j.id '
                'LEFT JOIN job_delegations d ON d.job_id=j.id LEFT JOIN delegation_plans p ON p.id=d.plan_id '
                'LEFT JOIN job_leases l ON l.job_id=j.id WHERE j.unit_id=? AND j.id>? ORDER BY j.id LIMIT ?',(unit_id,after,limit+1))]
            from .task_queries import task_page_in
            page=task_page_in(db,unit,task_after=task_after,task_limit=task_limit)
            outputs=list({ref['output_id']:ref for contribution in page['contributions'].values() for ref in contribution['outputs']}.values())
            return {**page,'unit_id':unit_id,'version':unit['version'],'builder':dict(builder) if builder else None,
                    'jobs':jobs[:limit],'next_cursor':jobs[limit-1]['id'] if len(jobs)>limit else None,'outputs':outputs,'charged_failures':unit['charged_failures'],'members':unit['members']}

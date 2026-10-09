"""Approved compositions and one logical builder's tracked execution tree.

Every operation shares the existing unit version, decision journal, failure budget
and execution leases. This module performs no provider, filesystem or GitHub I/O.
"""
from dataclasses import asdict
import json
import re

from .models import Candidate, Command, TransitionError, VersionConflict, InvalidDelegationPlan, complete_requirements
from .transitions import Lifecycle, _id, _json, _unit, _gates, _charges, applicable_gates, manifest_hash, project_unit
from .delivery_inputs import members_in, member_pins_in, dependencies_in, current_output_in, output_ref, requirements_hash_in


class Delivery(Lifecycle):
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
            if any(kinds.get(login) != 'Bot' for login in json.loads(issue['assignees_json'])) or set(json.loads(issue['labels_json'])) & {'hold','blocked'}:
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
                if any(gate['code'] not in {'dependency','verification_human','verification_ci','approval_required','merge_checks','budget_exhausted'}
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
        db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,?,'queued',?,?)", (command.unit_id,repository_id,base_branch,self.clock(),self.clock()))
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
        if stage not in {'planning','integration'} or not all(isinstance(value,str) and value for value in (base_oid,policy_hash)):
            raise TransitionError('builder input requires a stage and exact base/policy identities')
        manifest = {'membership_generation':unit['membership_generation'], 'members':member_pins_in(db,unit),
                    'requirements_hash':requirements_hash_in(db,unit), 'base_oid':base_oid,'policy_hash':policy_hash,
                    'dependencies':{member['id']:dependencies_in(db,unit,member['id']) for member in members_in(db,unit['id'])}}
        manifest['publication_target'] = json.loads(db.execute('SELECT target_json FROM unit_publication_targets WHERE unit_id=?',(unit['id'],)).fetchone()[0])
        manifest['existing_candidates'] = [dict(row) for row in db.execute(
            'SELECT s.source_unit_id,c.id AS candidate_id,c.pull_number,c.branch,c.head_oid,c.base_oid,c.requirements_hash '
            'FROM unit_composition_sources s JOIN candidates c ON c.id=s.source_candidate_id '
            'WHERE s.unit_id=? ORDER BY s.source_unit_id',(unit['id'],))]
        if stage == 'planning':
            current = [(member['member_id'],current_output_in(db,unit,member['member_id'],base_oid=base_oid,policy_hash=policy_hash)) for member in manifest['members']]
            manifest['accepted_outputs'] = [output_ref(output) for _,output in current if output is not None]
            manifest['remaining_members'] = [member_id for member_id,output in current if output is None]
        if stage == 'integration':
            outputs = [current_output_in(db,unit,member['member_id'],base_oid=base_oid,policy_hash=policy_hash)
                       for member in manifest['members']]
            if not outputs or any(output is None for output in outputs):
                raise TransitionError('integration requires every current member output')
            manifest['part_outputs'] = [output_ref(output) for output in outputs]
        return manifest

    @staticmethod
    def _plan_in(db, unit, plan_id):
        row = db.execute('SELECT p.*,a.input_manifest_json,a.assignment_generation,a.membership_generation,a.budget_generation,j.cleanup,j.outcome '
            'FROM delegation_plans p JOIN unit_builders b ON b.id=p.builder_id JOIN jobs j ON j.id=p.parent_job_id '
            'JOIN attempts a ON a.id=j.attempt_id WHERE p.id=? AND b.unit_id=?', (plan_id,unit['id'])).fetchone()
        if (row is None or not row['cleanup'] or row['outcome'] != 'planned'
                or row['assignment_generation'] != unit['fencing_generation']
                or row['membership_generation'] != unit['membership_generation'] or row['budget_generation'] != unit['budget_generation']):
            raise TransitionError('delegation requires a current saved plan and cleaned planning execution')
        return dict(row)

    @staticmethod
    def _child_manifest_in(db, unit, plan, key):
        entry = next((child for child in json.loads(plan['plan_json'])['children'] if child['key'] == key),None)
        if entry is None:
            raise TransitionError('child is outside the approved builder plan')
        member = db.execute('SELECT * FROM unit_issues WHERE id=? AND unit_id=? AND left_at IS NULL', (entry['member_id'],unit['id'])).fetchone()
        if member is None:
            raise TransitionError('delegated member is no longer live')
        parent = json.loads(plan['input_manifest_json'])
        prerequisites = []
        for predecessor in dependencies_in(db,unit,member['id']):
            output = current_output_in(db,unit,predecessor,base_oid=parent['base_oid'],policy_hash=parent['policy_hash'])
            if output is None:
                raise TransitionError('child prerequisite output is not accepted for this base and policy')
            prerequisites.append(output_ref(output))
        return {'requirements_revision_id':member['requirements_revision_id'], 'base_oid':parent['base_oid'],
                'policy_hash':parent['policy_hash'],'prerequisite_outputs':prerequisites,
                'delegation_plan_id':plan['id'],'delegation_key':key,'scope':entry['scope']}

    def _reserve_in(self, db, command, unit, *, slot, execution_fence, input_manifest, member_id=None, launch=None):
        if not isinstance(slot,str) or not slot or not isinstance(execution_fence,str) or not execution_fence:
            raise TransitionError('assignment requires a capacity slot and execution fence')
        if _charges(db,unit) >= 3:
            raise TransitionError('unit failure budget is exhausted')
        attempt_id,job_id = _id(command.request_id,'attempt'),_id(command.request_id,'job')
        db.execute('INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (attempt_id,unit['id'],member_id,unit['membership_generation'],unit['fencing_generation'],_json(input_manifest),unit['budget_generation'],self.clock()))
        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,created_at) VALUES(?,?,?,?,'build','reserved',?)", (job_id,unit['id'],attempt_id,member_id,self.clock()))
        db.execute('INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)', (slot,job_id,execution_fence,self.clock()))
        self._event(db,command,'attempt_reserved',{'input_manifest':input_manifest,'member_id':member_id},job_id=job_id)
        return {'job_id':job_id,'attempt_id':attempt_id}

    def reserve_builder(self, command, *, stage, slot, execution_fence, input_manifest, launch=None):
        payload = dict(stage=stage,slot=slot,execution_fence=execution_fence,input_manifest=input_manifest,
                       launch=launch.payload() if launch else None)
        def change(db,unit):
            self._live(unit)
            if len(members_in(db,unit['id'])) < 2 or unit['state'] not in {'queued','building','integrating'}:
                raise TransitionError('owning builder reservation requires a grouped delivery in code work')
            self._quiescent(db,unit['id'])
            expected = self._builder_manifest_in(db,unit,stage,input_manifest.get('base_oid'),input_manifest.get('policy_hash'))
            if input_manifest != expected:
                raise TransitionError('builder manifest differs from current approved membership or outputs')
            if applicable_gates(db,unit,candidate_id=unit['current_candidate_id']):
                raise TransitionError('an applicable unit gate blocks the owning builder')
            if stage == 'integration' and any(applicable_gates(db,unit,member['id'],unit['current_candidate_id']) for member in members_in(db,unit['id'])):
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
            target = 'integrating' if stage == 'integration' else 'building'
            db.execute('UPDATE units SET state=? WHERE id=?',(target,unit['id']))
            self._event(db,command,'phase_changed',{'before':unit['state'],'after':target,'builder_id':builder_id},job_id=job['job_id'])
            return {**job,'builder_id':builder_id}
        return self._apply(command,'reserve_builder',payload,change)

    def save_plan(self,command,job_id,*,execution_fence,input_manifest,plan,evidence):
        payload = dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,plan=plan,evidence=evidence)
        def change(db,unit):
            job,current = self._job(db,unit,job_id,execution_fence,input_manifest)
            owner = db.execute("SELECT * FROM builder_assignments WHERE job_id=? AND stage='planning'",(job_id,)).fetchone()
            if owner is None:
                raise TransitionError('only the owning planning assignment can save a delegation plan')
            if not current:
                self._result(db,command,job,evidence,'stale_plan')
                return {'accepted':False}
            if not isinstance(plan,dict) or set(plan) != {'children'} or not isinstance(plan['children'],list) or len(_json(plan).encode()) > 65536:
                raise InvalidDelegationPlan('delegation plan must be a bounded children object')
            members = {member['id']:member for member in members_in(db,unit['id'])}
            keys,assigned = set(),set()
            for child in plan['children']:
                if (not isinstance(child,dict) or set(child) != {'key','member_id','scope','dependencies'}
                        or not isinstance(child['key'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',child['key']) or child['key'] in keys
                        or not isinstance(child['member_id'],str) or child['member_id'] not in members or child['member_id'] in assigned
                        or not isinstance(child['scope'],str) or not child['scope'].strip()
                        or child['dependencies'] != dependencies_in(db,unit,child['member_id'])):
                    raise InvalidDelegationPlan('child scope or dependencies differ from approved membership')
                keys.add(child['key']); assigned.add(child['member_id'])
            remaining = {member_id for member_id in members if current_output_in(db,unit,member_id,
                         base_oid=input_manifest['base_oid'],policy_hash=input_manifest['policy_hash']) is None}
            if assigned != remaining:
                raise InvalidDelegationPlan('plan must cover every remaining member exactly once')
            plan_id = _id(command.request_id,'plan')
            db.execute('INSERT INTO delegation_plans VALUES(?,?,?,?,?,?)',(plan_id,owner['builder_id'],job_id,manifest_hash(input_manifest),_json(plan),self.clock()))
            self._result(db,command,job,{'plan_id':plan_id,'plan':plan,'evidence':evidence},'planned')
            return {'accepted':True,'plan_id':plan_id,'builder_id':owner['builder_id']}
        return self._apply(command,'save_plan',payload,change)

    def delegate(self,command,plan_id,delegation_key,*,slot,execution_fence,input_manifest,worktree_key,launch=None):
        payload = dict(plan_id=plan_id,delegation_key=delegation_key,slot=slot,execution_fence=execution_fence,
                       input_manifest=input_manifest,worktree_key=worktree_key,launch=launch.payload() if launch else None)
        def change(db,unit):
            self._live(unit)
            if unit['state'] != 'building':
                raise TransitionError('children can run only during the delivery build phase')
            plan = self._plan_in(db,unit,plan_id)
            expected = self._child_manifest_in(db,unit,plan,delegation_key)
            entry = next(child for child in json.loads(plan['plan_json'])['children'] if child['key']==delegation_key)
            if input_manifest != expected or not isinstance(worktree_key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',worktree_key):
                raise TransitionError('child inputs or isolated worktree identity are invalid')
            if applicable_gates(db,unit,entry['member_id']):
                raise TransitionError('an applicable scoped gate blocks this child')
            if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0 AND (member_id IS NULL OR member_id=?)',(unit['id'],entry['member_id'])).fetchone():
                raise TransitionError('member or owning execution still requires cleanup')
            job = self._reserve_in(db,command,unit,slot=slot,execution_fence=execution_fence,input_manifest=input_manifest,member_id=entry['member_id'],launch=launch)
            db.execute('INSERT INTO job_delegations VALUES(?,?,?,?)',(job['job_id'],plan_id,delegation_key,worktree_key))
            if launch is not None:
                from .executions import insert_launch
                insert_launch(db,job['job_id'],slot,execution_fence,launch.payload(),self.clock())
            db.execute("UPDATE unit_issues SET state='building' WHERE id=?",(entry['member_id'],))
            return {**job,'builder_id':plan['builder_id'],'parent_job_id':plan['parent_job_id'],'member_id':entry['member_id']}
        return self._apply(command,'delegate',payload,change)

    def accept_part(self,command,job_id,*,execution_fence,input_manifest,commit_oid,evidence):
        payload = dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,commit_oid=commit_oid,evidence=evidence)
        def change(db,unit):
            job,current = self._job(db,unit,job_id,execution_fence,input_manifest)
            delegation = db.execute('SELECT * FROM job_delegations WHERE job_id=?',(job_id,)).fetchone()
            if delegation is None:
                raise TransitionError('part output requires a tracked child assignment')
            if not current:
                self._result(db,command,job,evidence,'stale_success')
                return {'accepted':False}
            plan = self._plan_in(db,unit,delegation['plan_id'])
            expected = self._child_manifest_in(db,unit,plan,delegation['delegation_key'])
            if input_manifest != expected:
                raise TransitionError('prerequisite outputs changed before part acceptance')
            if (not isinstance(commit_oid,str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',commit_oid)
                    or not isinstance(evidence,dict) or evidence.get('verified') is not True
                    or evidence.get('commit_oid') != commit_oid or evidence.get('base_oid') != input_manifest['base_oid']
                    or evidence.get('input_manifest_hash') != manifest_hash(input_manifest)):
                raise TransitionError('part output needs independently verified exact commit and input evidence')
            gates = applicable_gates(db,unit,job['member_id'])
            if any(gate['code'] not in {'cleanup_pending','result_pending'} or json.loads(gate['details_json']).get('job_id') != job_id for gate in gates):
                raise TransitionError('a scoped gate blocks part acceptance')
            from .artifacts import Artifacts
            capture = json.loads(Artifacts(self.store).read(job_id,'worker_capture').content)
            bundle = capture.get('bundle')
            chunks = db.execute("SELECT sequence,COALESCE(raw_size,size) AS raw_size FROM job_artifacts "
                                "WHERE job_id=? AND kind='worktree_bundle' AND expired_at IS NULL ORDER BY sequence", (job_id,)).fetchall()
            if (not isinstance(bundle,dict) or evidence.get('bundle_digest') != bundle.get('digest')
                    or not chunks or len(chunks) != bundle.get('chunks')
                    or [chunk['sequence'] for chunk in chunks] != list(range(len(chunks)))
                    or sum(chunk['raw_size'] for chunk in chunks) != bundle.get('size')):
                raise TransitionError('accepted output must retain its exact captured worktree bundle')
            output_id = _id(command.request_id,'output')
            db.execute('INSERT INTO part_outputs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (output_id,unit['id'],job['member_id'],job['attempt_id'],input_manifest['requirements_revision_id'],unit['membership_generation'],commit_oid,input_manifest['base_oid'],manifest_hash(input_manifest),self.clock()))
            self._result(db,command,job,evidence,'success')
            db.execute("UPDATE unit_issues SET state='built' WHERE id=?",(job['member_id'],))
            self._event(db,command,'part_accepted',{'output_id':output_id,'member_id':job['member_id'],'commit_oid':commit_oid},job_id=job_id)
            return {'accepted':True,'output_id':output_id}
        return self._apply(command,'accept_part',payload,change)

    @staticmethod
    def _publication_target_in(manifest, candidate):
        target = manifest['publication_target']
        if target['kind'] == 'existing':
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
            if input_manifest != expected or unit['state'] != 'integrating':
                raise TransitionError('combined candidate inputs are no longer current')
            if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND id!=? AND cleanup=0',(unit['id'],job_id)).fetchone():
                raise TransitionError('all children must be cleaned before combined publication')
            if any(g['code'] not in {'result_pending','cleanup_pending'} or json.loads(g['details_json']).get('job_id') != job_id
                   for g in applicable_gates(db,unit,candidate_id=unit['current_candidate_id'])):
                raise TransitionError('a gate blocks combined publication')
            if (candidate.requirements_hash != expected['requirements_hash'] or candidate.input_manifest_hash != manifest_hash(expected)
                    or candidate.base_oid != expected['base_oid'] or candidate.policy_hash != expected['policy_hash']
                    or not isinstance(evidence,dict) or evidence.get('verified') is not True
                    or any(evidence.get(key) != getattr(candidate,key) for key in ('pull_number','head_oid','base_oid'))):
                raise TransitionError('publication must independently verify the exact integrated candidate')
            self._publication_target_in(expected,candidate)
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

    def cancel_builder(self,command,*,evidence):
        if not isinstance(evidence,dict) or not evidence:
            raise TransitionError('cancelling a builder requires a recorded reason')
        def change(db,unit):
            self._live(unit)
            self._fence(db,command)
            return {'charged':False,'cleanup_required':True}
        return self._apply(command,'cancel_builder',{'evidence':evidence},change)

    def drop_member(self,command,member_id,*,membership_generation,dependent_decisions,evidence):
        """An explicit drop retains history, shared charges and unaffected outputs."""
        payload=dict(member_id=member_id,membership_generation=membership_generation,
                     dependent_decisions=dependent_decisions,evidence=evidence)
        def change(db,unit):
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
            if removed is None or len(members)<2:
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
            old_candidate=unit['current_candidate_id']
            self._fence(db,command)
            for gate in _gates(db,unit['id']):
                if gate['member_id']==member_id or gate['candidate_id'] is not None:
                    self._resolve(db,command,gate['id'],{'dropped_member_id':member_id},resolution='superseded')
            for affected_id in affected:
                db.execute('INSERT OR IGNORE INTO part_output_invalidations(output_id,reason,evidence_json,created_at) '
                    "SELECT id,'member_dropped',?,? FROM part_outputs WHERE member_id=?",(_json({'member_id':member_id}),self.clock(),affected_id))
                db.execute("UPDATE unit_issues SET state='pending' WHERE id=?",(affected_id,))
            for dependent in direct:
                member=next(member for member in members if member['id']==dependent)
                db.execute("INSERT OR IGNORE INTO issue_dependencies(id,issue_id,requirements_revision_id,prerequisite_issue_id,source,evidence,created_at) "
                    "VALUES(?,?,?,?,'operator',?,?)",(_id(command.request_id,'external:'+dependent),member['issue_id'],member['requirements_revision_id'],removed['issue_id'],'Explicit dependency retained after member drop',self.clock()))
            db.execute("UPDATE unit_issues SET left_at=?,exit_reason='dropped',state='removed' WHERE id=?",(self.clock(),member_id))
            db.execute("UPDATE units SET membership_generation=membership_generation+1,current_candidate_id=NULL,state='building' WHERE id=?",(unit['id'],))
            for member,predecessor in edges:
                db.execute('INSERT INTO member_dependencies VALUES(?,?,?,?)',(unit['id'],member,predecessor,unit['membership_generation']+1))
            issue=dict(db.execute('SELECT * FROM issues WHERE id=?',(removed['issue_id'],)).fetchone())
            if issue['github_state'] != 'open':
                raise TransitionError('closed members need a closure decision before standalone admission')
            released_id=_id(command.request_id,'released-unit')
            released_member=self._create_unit(db,command,released_id,issue,unit['base_branch'],removed['requirements_revision_id'])
            inherited={row[0] for row in db.execute('SELECT id FROM attempts WHERE unit_id=? AND budget_generation=?',(unit['id'],unit['budget_generation']))}
            inherited.update(row[0] for row in db.execute('SELECT attempt_id FROM unit_budget_lineage WHERE unit_id=? AND budget_generation=?',(unit['id'],unit['budget_generation'])))
            for attempt in inherited: db.execute('INSERT INTO unit_budget_lineage VALUES(?,1,?)',(released_id,attempt))
            released_command=Command(command.request_id,command.actor_id,released_id,1)
            for gate in _gates(db,unit['id']):
                if gate['code'] in {'operator_pause','operator_backlog'}:
                    self._open_gate(db,released_command,gate['code'],{'source_unit_id':unit['id'],'source_gate_id':gate['id']})
            # Moving failed work is not permission to reset its original shared budget.
            if _charges(db,_unit(db,released_id))>=3:
                self._open_gate(db,released_command,'budget_exhausted',{'limit':3,'source':'member_drop'})
            if old_candidate:
                self._event(db,command,'candidate_superseded',{'candidate_id':old_candidate,'reason':'member_dropped'})
            self._event(db,command,'membership_changed',{'member_id':member_id,'released_unit_id':released_id,'affected_member_ids':sorted(affected)},issue_id=removed['issue_id'])
            return {'released':{'unit_id':released_id,'member_id':released_member,'issue_id':removed['issue_id']},
                    'affected_member_ids':sorted(affected),'membership_generation':unit['membership_generation']+1}
        return self._apply(command,'drop_member',payload,change)


class DeliveryQueries:
    def __init__(self,store):
        self.store = store

    def builder_manifest(self,unit_id,stage,*,base_oid,policy_hash):
        with self.store.read_snapshot() as db:
            return Delivery._builder_manifest_in(db,_unit(db,unit_id),stage,base_oid,policy_hash)

    def child_manifest(self,plan_id,key):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT b.unit_id FROM delegation_plans p JOIN unit_builders b ON b.id=p.builder_id WHERE p.id=?',(plan_id,)).fetchone()
            if row is None:
                raise KeyError(plan_id)
            unit = _unit(db,row[0])
            return Delivery._child_manifest_in(db,unit,Delivery._plan_in(db,unit,plan_id),key)

    def assignments(self,unit_id,*,base_oid,policy_hash,limit=100):
        from .queries import _limit
        limit = _limit(limit)
        with self.store.read_snapshot() as db:
            unit = _unit(db,unit_id)
            def pending(reason):
                return {'unit_id':unit_id,'version':unit['version'],'items':[],'pending_reason':reason}
            if unit['state'] not in {'queued','building','integrating'}:
                return pending('not_building')
            members = members_in(db,unit_id)
            if len(members)<2:
                return pending('singleton')
            if _charges(db,unit)>=3:
                return pending('budget_exhausted')
            if applicable_gates(db,unit,candidate_id=unit['current_candidate_id']):
                return pending('unit_blocked')
            active = [dict(row) for row in db.execute('SELECT id,member_id FROM jobs WHERE unit_id=? AND cleanup=0',(unit_id,))]
            if any(job['member_id'] is None for job in active):
                return pending('builder_cleanup_pending')
            expected = Delivery._builder_manifest_in(db,unit,'planning',base_oid,policy_hash)
            complete = all(current_output_in(db,unit,m['id'],base_oid=base_oid,policy_hash=policy_hash) for m in members)
            if complete:
                if active:
                    return pending('child_cleanup_pending')
                if any(applicable_gates(db,unit,m['id']) for m in members):
                    return pending('member_blocked')
                return {'unit_id':unit_id,'version':unit['version'],'pending_reason':None,'items':[{
                    'stage':'integration','input_manifest':Delivery._builder_manifest_in(db,unit,'integration',base_oid,policy_hash)}]}
            plan = db.execute('SELECT p.*,a.input_manifest_json,a.assignment_generation,a.membership_generation,a.budget_generation,j.cleanup,j.outcome '
                'FROM delegation_plans p JOIN unit_builders b ON b.id=p.builder_id JOIN jobs j ON j.id=p.parent_job_id '
                'JOIN attempts a ON a.id=j.attempt_id WHERE b.unit_id=? ORDER BY p.created_at DESC,p.id DESC LIMIT 1',(unit_id,)).fetchone()
            valid_plan = bool(plan and plan['cleanup'] and plan['outcome']=='planned'
                and plan['assignment_generation']==unit['fencing_generation'] and plan['membership_generation']==unit['membership_generation']
                and plan['budget_generation']==unit['budget_generation']
                and {key:value for key,value in json.loads(plan['input_manifest_json']).items() if key not in {'accepted_outputs','remaining_members'}}
                    == {key:value for key,value in expected.items() if key not in {'accepted_outputs','remaining_members'}})
            items,needs_plan = [],not valid_plan
            if valid_plan:
                for entry in json.loads(plan['plan_json'])['children']:
                    if current_output_in(db,unit,entry['member_id'],base_oid=base_oid,policy_hash=policy_hash):
                        continue
                    if applicable_gates(db,unit,entry['member_id']):
                        continue
                    if any(job['member_id']==entry['member_id'] for job in active):
                        continue
                    assigned = db.execute('SELECT 1 FROM job_delegations WHERE plan_id=? AND delegation_key=?',(plan['id'],entry['key'])).fetchone()
                    if assigned:
                        needs_plan=True
                        continue
                    items.append({'stage':'child','plan_id':plan['id'],'key':entry['key'],'member_id':entry['member_id'],
                        'worktree_key':_id(plan['id'],'worktree:'+entry['key']),
                        'input_manifest':Delivery._child_manifest_in(db,unit,dict(plan),entry['key'])})
            if items:
                return {'unit_id':unit_id,'version':unit['version'],'items':items[:limit],'pending_reason':None}
            if not active and needs_plan and any(not applicable_gates(db,unit,m['id']) for m in members):
                return {'unit_id':unit_id,'version':unit['version'],'items':[{'stage':'planning','input_manifest':expected}],'pending_reason':None}
            return pending('children_running' if active else 'member_blocked')

    def next_assignment(self,unit_id,*,base_oid,policy_hash):
        result = self.assignments(unit_id,base_oid=base_oid,policy_hash=policy_hash,limit=1)
        return {key:value for key,value in result.items() if key!='items'} | {'assignment':result['items'][0] if result['items'] else None}

    def tree(self,unit_id,*,after='',limit=100):
        from .queries import _limit
        limit = _limit(limit)
        if not isinstance(after,str):
            raise ValueError('execution tree cursor must be a job ID')
        with self.store.read_snapshot() as db:
            unit = project_unit(db,unit_id)
            builder = db.execute('SELECT * FROM unit_builders WHERE unit_id=?',(unit_id,)).fetchone()
            jobs = [dict(row) for row in db.execute('SELECT j.id,j.attempt_id,j.member_id,j.role,j.phase,j.outcome,j.fenced,j.cleanup,'
                'a.stage,d.plan_id,d.delegation_key,d.worktree_key,p.parent_job_id,l.slot '
                'FROM jobs j LEFT JOIN builder_assignments a ON a.job_id=j.id '
                'LEFT JOIN job_delegations d ON d.job_id=j.id LEFT JOIN delegation_plans p ON p.id=d.plan_id '
                'LEFT JOIN job_leases l ON l.job_id=j.id WHERE j.unit_id=? AND j.id>? ORDER BY j.id LIMIT ?',(unit_id,after,limit+1))]
            outputs = [output_ref(output) for member in members_in(db,unit_id)
                       if (output:=current_output_in(db,unit,member['id'])) is not None]
            return {'unit_id':unit_id,'version':unit['version'],'builder':dict(builder) if builder else None,
                    'jobs':jobs[:limit],'next_cursor':jobs[limit-1]['id'] if len(jobs)>limit else None,'outputs':outputs,'charged_failures':unit['charged_failures'],'members':unit['members']}

"""Accept every typed build claim under one captured assignment transaction."""
from .worker_claims import WorkerClaims
from .models import TransitionError


class BuildClaims(WorkerClaims):
    def defer(self, command, job_id, *, execution_fence, input_manifest, report, dependencies):
        from .executions import ExecutionQueries
        snapshot=ExecutionQueries(self.store).job(job_id)
        payload=dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,
                     report=report,dependencies=dependencies)
        def change(db,unit):
            from ..factory.build_results import validate
            job,current=self._job(db,unit,job_id,execution_fence,input_manifest)
            # Controller validation is repeated against the immutable launch.
            pins=list(db.execute('SELECT p.member_id,p.requirements_revision_id,i.github_number FROM job_members p '
                'JOIN unit_issues m ON m.id=p.member_id JOIN issues i ON i.id=m.issue_id WHERE p.job_id=? ORDER BY p.member_id',(job_id,)))
            validate(report,snapshot)
            evidence={'report':report,'capture_job_id':job_id,'dependency_observations':dependencies}
            if not current:
                self._result(db,command,job,evidence,'stale_deferred')
                return {'accepted':False,'charged':False}
            if report['outcome']!='cannot_complete' or not report['claims'] or not pins:
                raise TransitionError('Build claims require an incomplete captured assignment')
            capture=db.execute("SELECT 1 FROM job_execution_state WHERE job_id=? AND captured_at IS NOT NULL",(job_id,)).fetchone()
            from .steward import artifact_in
            if not capture or validate(artifact_in(db,job_id,'worker_capture').get('result'),snapshot)!=report:
                raise TransitionError('Build claims must match the captured result')
            environment=next((c for c in report['claims'] if c['type']=='environment'),None)
            outcome={'accepted':True,'charged':False,'gate_ids':[]}
            if environment:
                details={'reason':environment['reason'],'environment_id':environment['environment_id'],
                    'required_services':environment['services'],'summary':environment['evidence'],
                    'operator_summary':report['operator_summary']}
                outcome=self.defer_claim_in(db,command,unit,job_id,execution_fence=execution_fence,
                    input_manifest=input_manifest,evidence=evidence,details=details)
            else:
                self._result(db,command,job,evidence,'deferred')
            gates=list(outcome.get('gate_ids',[]))
            references=[fact['reference'] for fact in dependencies]
            claimed={(c['repository_id'],c['issue_number']) for c in report['claims'] if c['type']=='wait_on_issue'}
            if claimed!={(f['repository_id'],f['number']) for f in dependencies}:
                raise TransitionError('Every dependency claim needs its exact observed issue')
            decisions=[c for c in report['claims'] if c['type'] in {'needs_decision','too_large'}]
            requirements_by_member={pin['member_id']:pin['requirements_revision_id'] for pin in pins}
            for code,details in ([('dependency',{'references':references,'issues':[],
                    'dependency_observations':dependencies})] if references else []) + (
                    [('decision_required',{'claims':decisions})] if decisions else []):
                # Each captured job owns its claims. An unrelated operator/member
                # gate keeps its own identity and release authority.
                gates.append(self._open_gate(db,command,code,{**details,'source':'build_claim','job_id':job_id,
                    'requirements_by_member':requirements_by_member},job_id=job_id))
            for fact in dependencies:
                identity=tuple(fact[key] for key in ('repository_id','github_id','number','kind'))
                binding=db.execute('SELECT * FROM prerequisite_identities WHERE reference=?',(fact['reference'],)).fetchone()
                if binding and tuple(binding[key] for key in ('repository_id','github_id','number','kind'))!=identity:
                    raise TransitionError('Dependency claim conflicts with its immutable GitHub identity')
                if binding is None:
                    db.execute('INSERT INTO prerequisite_identities VALUES(?,?,?,?,?,?)',(fact['reference'],*identity,self.clock()))
            return {**outcome,'gate_ids':gates,'gate_id':gates[0] if len(gates)==1 else None}
        return self._apply(command,'defer_build_claims',payload,change)

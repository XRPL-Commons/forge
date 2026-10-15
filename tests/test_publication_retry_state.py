"""Steward retries cannot abandon an unsettled publication or result."""
import json
import unittest

from forge.store.executions import Launch
from forge.store.models import TransitionError
from tests import test_steward_execution as fixtures


class PublicationRetryStateTests(unittest.TestCase):
    def setup_retry(self,status,*,consume_result=True):
        fixture=fixtures.StewardExecutionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        f=fixture.f;cmd=f.command()
        with fixture.store.read_snapshot() as db:
            revision=db.execute('SELECT requirements_revision_id FROM unit_issues WHERE id=?',(f.member,)).fetchone()[0]
        manifest={'requirements_revision_id':revision,'base_oid':'a'*40,'policy_hash':'policy'}
        f.life._apply(cmd,'test_release_initial_stall',{},lambda db,unit:
            (f.life._resolve(db,cmd,f.gate,{'fixture':'before publication'}) or {}))
        build=f.life.reserve_job(f.command(),'build',slot='builder',execution_fence='build-fence',
            input_manifest=manifest,member_id=f.member,
            launch=Launch('forge/build','build[bot]','codex',1000,{},{}))
        f.life.collect(f.command(),build['job_id'],execution_fence='build-fence',capture={'result':None,'logs':''})
        f.life.verify_cleanup(f.command(),build['job_id'],execution_fence='build-fence',
            evidence={'process_stopped':True,'resources_released':True})
        cmd=f.command()
        with fixture.store.write() as db:
            if consume_result:
                job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(build['job_id'],)).fetchone())
                f.life._result(db,cmd,job,{},'deferred')
            db.execute('INSERT INTO publication_intents(id,job_id,unit_id,execution_fence,input_manifest_json,'
                'output_digest,output_json,target_json,report_json,status,created_at,updated_at) '
                "VALUES('publication',?,'u','build-fence',?,?,'{}','{}','{}',?,100,100)",
                (build['job_id'],json.dumps(manifest),'a'*64,status))
            db.execute("UPDATE units SET state='publishing' WHERE id='u'")
        f.gate=f.open_gate('forge_problem',{'source':'scheduling','reason':'stalled','owner':'dispatch'})
        job=fixture.reserve();identifier,output=fixture.accept(job)
        fixture.exec.decision_wait(f.command(),identifier);f.select(identifier,output)
        return fixture,identifier

    def test_settled_publication_retry_returns_to_building(self):
        fixture,identifier=self.setup_retry('superseded')
        fixture.apply(identifier)
        self.assertEqual(fixture.f.view()['state'],'building')
        with fixture.store.read_snapshot() as db:
            plan=json.loads(db.execute("SELECT retry_plan_json FROM unit_context WHERE unit_id='u'").fetchone()[0])
            self.assertEqual(plan['role'],'build')
            self.assertEqual(plan['action'],'retry')
        from forge.factory.dispatch_controller import DispatchController, PreparedDispatch
        from tests.test_store_sqlite_supervisor import GitHub, Runner
        driver=DispatchController(fixture.store,Runner(GitHub()),config=fixture.config,
            prepare=lambda snapshot,role:PreparedDispatch('a'*40,'build[bot]',fixture.config),
            admission=lambda snapshot:True,clock=lambda:fixture.f.now)
        self.assertTrue(driver.step('u')['reserved'])

    def test_uncertain_publication_cannot_be_retried_as_new_code_work(self):
        fixture,identifier=self.setup_retry('uncertain')
        with self.assertRaisesRegex(TransitionError,'[Pp]ublication'):
            fixture.apply(identifier)
        self.assertEqual(fixture.f.view()['state'],'publishing')

    def test_confirmed_publication_cannot_skip_its_unconsumed_result(self):
        with self.assertRaisesRegex(TransitionError,'independent gate'):
            self.setup_retry('confirmed',consume_result=False)

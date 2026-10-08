import json
import unittest
from forge.factory.store_views import PipelineViews
from tests import test_store_result_controller as fixtures


class ResultSummaryTests(unittest.TestCase):
    setUp=fixtures.ResultControllerTests.setUp
    command=fixtures.ResultControllerTests.command
    view=fixtures.ResultControllerTests.view
    reserve=fixtures.ResultControllerTests.reserve
    capture=fixtures.ResultControllerTests.capture
    report=fixtures.ResultControllerTests.report
    publish=fixtures.ResultControllerTests.publish

    def summary(self):
        return PipelineViews(self.store,clock=lambda:100).factory(issue='i')['issues'][0]['last_summary']

    def test_published_build_has_plain_language_summary_without_loading_logs(self):
        job=self.reserve();self.publish(job)
        self.capture(job,self.report(job,headline='Change published',plain='The change is ready for review.'))
        self.assertTrue(self.controller.step(job)['accepted'])
        self.assertEqual(self.summary(),{'headline':'Change published','plain':'The change is ready for review.','role':'build','at':100})

    def test_native_decision_keeps_current_summary(self):
        job=self.reserve()
        decision={'question':'Choose an approach','options':['A','B'],'recommendation':'A','evidence':'Two supported approaches'}
        self.capture(job,self.report(job,'needs_decision',headline='Operator decision',plain='Choose the next approach.',decision=decision))
        self.assertTrue(self.controller.step(job)['accepted'])
        self.assertEqual(self.summary()['headline'],'Operator decision')
        detail=PipelineViews(self.store,clock=lambda:100).issues(key='i')['issue']
        self.assertEqual(detail['summary']['headline'],'Operator decision')

    def test_failed_build_keeps_current_summary(self):
        job=self.reserve()
        self.capture(job,self.report(job,'unable',headline='Build failed',plain='Check the current result.'))
        self.assertTrue(self.controller.step(job)['charged'])
        self.assertEqual(self.summary()['headline'],'Build failed')

    def test_stale_or_invalid_report_does_not_replace_saved_summary(self):
        original={'headline':'Current accepted context','plain':'Keep this explanation.','role':'build','at':99}
        with self.store.write() as db:db.execute('INSERT INTO unit_context(unit_id,summary_json) VALUES(?,?)',('u',json.dumps(original)))
        job=self.reserve();self.capture(job,self.report(job,headline='Old worker result',plain='This result arrived late.'))
        self.life.pause(self.command())
        self.controller.step(job)
        self.assertEqual(self.summary(),original)
        # A current result with malformed worker prose also preserves context.
        self.life.resume(self.command());job=self.reserve();self.capture(job,{'bad':'report'})
        self.controller.step(job)
        self.assertEqual(self.summary(),original)

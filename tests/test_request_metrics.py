"""Measure real transport attempts while keeping credentials out of receipts."""
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from email.message import Message

from forge.app_access import AppClient, AccessError
from forge.request_metrics import RequestMetrics, RequestBudgetExhausted, request_scope


class RequestMetricsTests(unittest.TestCase):
    def test_request_uses_remaining_deadline_and_records_only_numeric_quota(self):
        headers=Message()
        for key,value in {'X-RateLimit-Limit':'5000','X-RateLimit-Remaining':'4999',
            'X-RateLimit-Reset':'12345','Authorization':'private-token'}.items():headers[key]=value
        response=Mock(status=200,headers=headers)
        response.read.return_value=b'{"ok":true}'
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        metrics=RequestMetrics()
        with patch('forge.app_access.build_opener',return_value=opener),patch('forge.request_metrics.time.monotonic',return_value=10):
            with request_scope(until=12,metrics=metrics):
                self.assertEqual(AppClient()._request('GET','/installation/repositories','private-token'),{'ok':True})
        self.assertEqual(opener.open.call_args.kwargs['timeout'],2)
        self.assertEqual(metrics.view(),dict(attempts=1,failures=0,elapsed_ms=0.0,quota=dict(limit=5000,remaining=4999,reset=12345)))
        self.assertNotIn('private',str(metrics.view()))

    def test_exhaustion_does_not_count_an_unattempted_request(self):
        metrics=RequestMetrics()
        with patch('forge.app_access.build_opener') as opener,patch('forge.request_metrics.time.monotonic',return_value=10):
            with request_scope(until=10,metrics=metrics):
                with self.assertRaises(RequestBudgetExhausted):AppClient()._request('GET','/rate_limit','private')
        opener.assert_not_called()
        self.assertEqual(metrics.attempts,0)

    def test_timeout_at_shared_deadline_is_deferred_not_an_availability_failure(self):
        ticks=iter([10,10,12,12,12])
        opener=Mock();opener.open.side_effect=TimeoutError('private')
        metrics=RequestMetrics()
        with patch('forge.app_access.build_opener',return_value=opener),patch('forge.request_metrics.time.monotonic',side_effect=lambda:next(ticks)):
            with request_scope(until=12,metrics=metrics):
                with self.assertRaises(RequestBudgetExhausted):AppClient()._request('GET','/private','secret')
        self.assertEqual((metrics.attempts,metrics.failures),(1,1))

    def test_error_counts_one_actual_attempt_without_recording_content(self):
        metrics=RequestMetrics()
        headers=Message();headers['X-RateLimit-Remaining']='0'
        error=HTTPError('https://api.github.com/private',403,'secret',headers,None)
        self.addCleanup(error.close)
        opener=Mock();opener.open.side_effect=error
        with patch('forge.app_access.build_opener',return_value=opener):
            with request_scope(metrics=metrics):
                with self.assertRaises(AccessError):AppClient()._request('GET','/private','secret')
        self.assertEqual((metrics.attempts,metrics.failures),(1,1))
        self.assertEqual(metrics.quota,{'remaining':0})
        self.assertNotIn('secret',str(metrics.view()))

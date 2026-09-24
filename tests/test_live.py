"""One bounded, credential-free read of the commit a domain is running."""
import json
import socket
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request

from forge.factory.live import COMMIT, LiveUnavailable, SameHost, read_commit


class Response:
    def __init__(self, body=b'', headers=None):
        self.body, self.headers, self.reads = body, headers or {}, []
        self.offset = 0

    def read(self, size=None):
        # A real response consumes what it hands out; the reader drains it.
        self.reads.append(size)
        end = len(self.body) if size is None else min(len(self.body), self.offset + size)
        chunk = self.body[self.offset:end]
        self.offset = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


class Slow(Response):
    """A host that answers, byte by byte, for ever."""

    def read(self, size=None):
        self.reads.append(size)
        return b'a'


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []
        if hasattr(error, 'close'):
            error.close()  # An HTTPError built without a body owns a temporary file.

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


COMMIT_VALUE = 'a' * 40


class LiveTests(unittest.TestCase):
    def health(self, payload, **fields):
        opener = Opener(Response(json.dumps(payload).encode()))
        value = read_commit('https://site.example/health', kind='health', opener=opener, **fields)
        return value, opener

    def test_a_health_endpoint_yields_the_commit_it_reports(self):
        value, opener = self.health({'commit': COMMIT_VALUE}, field='commit')
        self.assertEqual(value, COMMIT_VALUE)
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(timeout, 5)

    def test_a_dotted_field_walks_the_health_document(self):
        value, _ = self.health({'build': {'commit': COMMIT_VALUE}}, field='build.commit')
        self.assertEqual(value, COMMIT_VALUE)

    def test_a_slow_drip_response_ends_at_the_wall_clock(self):
        # The socket timeout bounds one read, not the whole body; observation
        # runs before dispatch, so a slow host must not hold the cycle.
        ticks = iter([0, 1, 2, 3, 4, 5, 6])
        with self.assertRaisesRegex(LiveUnavailable, 'too long to answer'):
            read_commit('https://example.org/health', kind='health', field='commit',
                        opener=Opener(Slow()), timeout=5, clock=lambda: next(ticks))

    def test_a_short_commit_is_accepted_and_returned_as_read(self):
        value, _ = self.health({'commit': 'abc1234'}, field='commit')
        self.assertEqual(value, 'abc1234')
        self.assertIsNone(self.health({'commit': 'abc123'}, field='commit')[0])
        self.assertIsNone(self.health({'commit': 'not-a-commit'}, field='commit')[0])
        self.assertIsNone(self.health({'commit': 12}, field='commit')[0])
        self.assertIsNone(self.health({'other': COMMIT_VALUE}, field='commit')[0])
        self.assertIsNone(self.health([COMMIT_VALUE], field='commit')[0])

    def test_a_header_exposure_asks_for_the_headers_only(self):
        opener = Opener(Response(b'', {'X-Build-Commit': COMMIT_VALUE.upper()}))
        value = read_commit('https://site.example/', kind='header', header='X-Build-Commit', opener=opener)
        self.assertEqual(value, COMMIT_VALUE)
        self.assertEqual(opener.requests[0][0].get_method(), 'HEAD')
        self.assertFalse(opener.response.reads)

    def test_a_missing_header_is_no_evidence_rather_than_a_failure(self):
        opener = Opener(Response(b'', {}))
        self.assertIsNone(read_commit('https://site.example/', kind='header', header='X-Build-Commit', opener=opener))

    def test_only_https_is_ever_contacted(self):
        for url in ('http://site.example/health', 'file:///etc/passwd', 'https:///health', 'ftp://site.example'):
            opener = Opener(Response(b'{}'))
            with self.assertRaises(LiveUnavailable):
                read_commit(url, kind='health', field='commit', opener=opener)
            self.assertFalse(opener.requests)

    def test_a_redirect_to_another_host_is_refused(self):
        handler = SameHost()
        request = Request('https://site.example/health')
        arguments = (request, None, 302, 'Found', {})
        self.assertIsNone(handler.redirect_request(*arguments, 'https://elsewhere.example/health'))
        self.assertIsNone(handler.redirect_request(*arguments, 'http://site.example/health'))
        self.assertIsNone(handler.redirect_request(*arguments, '/relative'))
        moved = handler.redirect_request(*arguments, 'https://site.example/healthz')
        self.assertEqual(moved.full_url, 'https://site.example/healthz')
        # The refusal reaches the caller as a failed read, never as a followed hop.
        opener = Opener(error=HTTPError('https://site.example/health', 302, 'Found', {}, None))
        with self.assertRaises(LiveUnavailable) as refused:
            read_commit('https://site.example/health', kind='health', field='commit', opener=opener)
        self.assertIn('302', str(refused.exception))

    def test_the_body_stops_at_the_size_limit(self):
        opener = Opener(Response(b'x' * 300))
        with self.assertRaises(LiveUnavailable):
            read_commit('https://site.example/health', kind='health', field='commit', opener=opener, limit=100)
        self.assertEqual(opener.response.reads, [101])
        opener = Opener(Response(json.dumps({'commit': COMMIT_VALUE}).encode()))
        read_commit('https://site.example/health', kind='health', field='commit', opener=opener)
        # Drained in chunks, and never past the cap.
        self.assertLessEqual(sum(opener.response.reads), 65537)
        self.assertTrue(all(size <= 8192 for size in opener.response.reads))

    def test_an_unreadable_answer_is_a_credential_free_failure(self):
        for error, expected in ((HTTPError('https://site.example/health', 503, 'Down', {}, None), '503'),
                                (URLError(socket.timeout('timed out')), 'in time'),
                                (URLError('name or service not known'), 'reached'),
                                (OSError('connection reset'), 'read')):
            opener = Opener(error=error)
            with self.assertRaises(LiveUnavailable) as failure:
                read_commit('https://site.example/health', kind='health', field='commit', opener=opener)
            self.assertIn(expected, str(failure.exception))
        opener = Opener(Response(b'<html>not json</html>'))
        with self.assertRaises(LiveUnavailable):
            read_commit('https://site.example/health', kind='health', field='commit', opener=opener)

    def test_the_request_carries_no_credentials_and_one_fixed_agent(self):
        value, opener = self.health({'commit': COMMIT_VALUE}, field='commit')
        request = opener.requests[0][0]
        self.assertEqual(request.get_header('User-agent'), 'forge-deployment-observer')
        self.assertEqual(set(request.headers), {'User-agent', 'Accept'})
        self.assertFalse(request.data)

    def test_the_commit_pattern_is_hexadecimal_only(self):
        self.assertTrue(COMMIT.fullmatch(COMMIT_VALUE))
        self.assertFalse(COMMIT.fullmatch('a' * 41))
        self.assertFalse(COMMIT.fullmatch('g' * 40))


if __name__ == '__main__':
    unittest.main()

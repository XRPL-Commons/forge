"""One bounded read of the commit a product is running, from its own domain.

Forge contacts a domain only to learn a commit: https only, no credentials, no
cross-host redirect, a five second timeout and a capped body. Anything else is a
failure the operator reads, never a silent "not deployed".
"""
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

AGENT = 'forge-deployment-observer'
# A full commit is forty hex; a build that exposes an abbreviation is still evidence.
COMMIT = re.compile(r'[0-9a-fA-F]{7,40}')


class LiveUnavailable(RuntimeError):
    """A credential-free reason suitable for the Activity page."""


class SameHost(HTTPRedirectHandler):
    """A product may redirect inside its own domain; another host is another product."""

    def redirect_request(self, request, fp, code, message, headers, newurl):
        target, origin = urlsplit(newurl), urlsplit(request.full_url)
        if target.scheme != 'https' or not target.hostname or target.hostname != origin.hostname:
            return None
        return super().redirect_request(request, fp, code, message, headers, newurl)


def commit(value):
    if not isinstance(value, str) or not COMMIT.fullmatch(value.strip()):
        return None
    return value.strip().lower()


def reason(error):
    if isinstance(error, HTTPError):
        return f'The deployment answered HTTP {error.code}'
    if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
        return 'The deployment did not answer in time'
    if isinstance(error, URLError):
        return 'The deployment domain could not be reached: ' + str(error.reason)[:200]
    return 'The deployment domain could not be read'


def field_value(document, field):
    for part in (field or '').split('.'):
        if not isinstance(document, dict) or part not in document:
            return None
        document = document[part]
    return document


def drain(response, limit, *, deadline, clock=time.monotonic):
    """Read a bounded body under a wall clock.

    The socket timeout bounds one read, not the whole response: a host that
    answers a byte at a time would otherwise hold the cycle for hours, and
    observation runs before dispatch.
    """
    body = bytearray()
    while len(body) <= limit:
        if clock() >= deadline:
            raise LiveUnavailable('The deployment took too long to answer')
        chunk = response.read(min(8192, limit + 1 - len(body)))
        if not chunk:
            break
        body += chunk
    return bytes(body)


def read_commit(url, *, kind, field=None, header=None, timeout=5, limit=65536, opener=None, clock=time.monotonic):
    """Return the commit the domain reports, or None when it reports none."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname:
        raise LiveUnavailable('A deployment is read over https only')
    request = Request(url, method='GET' if kind == 'health' else 'HEAD',
                      headers={'User-Agent': AGENT, 'Accept': 'application/json'})
    try:
        with (opener or build_opener(SameHost())).open(request, timeout=timeout) as response:
            if kind != 'health':
                return commit(response.headers.get(header))
            body = drain(response, limit, deadline=clock() + timeout, clock=clock)
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise LiveUnavailable(reason(error)) from None
    if len(body) > limit:
        raise LiveUnavailable('The deployment health response exceeded its size limit')
    try:
        document = json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        raise LiveUnavailable('The deployment health response is not a JSON document') from None
    return commit(field_value(document, field))

"""Scoped GitHub transport budgets and numeric evidence; no request content."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_scope = ContextVar('forge_github_request_scope', default=(float('inf'), ()))


class RequestBudgetExhausted(RuntimeError):
    pass


class RequestMetrics:
    def __init__(self):
        self.attempts = self.failures = 0
        self.elapsed_ms = 0.0
        self.quota = {}

    def record(self, status, elapsed, headers):
        self.attempts += 1
        self.failures += int(status is None or status >= 400)
        self.elapsed_ms += elapsed * 1000
        quota = {}
        for name in ('limit', 'remaining', 'reset'):
            value = headers.get('X-RateLimit-' + name) if headers is not None else None
            if isinstance(value, str) and value.isdecimal():
                quota[name] = int(value)
        if quota:
            self.quota = quota

    def view(self):
        return dict(attempts=self.attempts, failures=self.failures,
                    elapsed_ms=round(self.elapsed_ms, 3), quota=dict(self.quota))


def deadline():
    return _scope.get()[0]


@contextmanager
def request_scope(*, until=None, metrics=None):
    previous, observers = _scope.get()
    token = _scope.set((min(previous, until) if until is not None else previous,
                        observers + ((metrics,) if metrics is not None else ())))
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def request_attempt():
    """Yield bounded timeout and receipt fields for one actual HTTP request."""
    until, observers = _scope.get()
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise RequestBudgetExhausted('GitHub request budget exhausted')
    started = time.monotonic()
    receipt = {'status': None, 'headers': None}
    try:
        yield min(15, remaining), receipt
    finally:
        elapsed = time.monotonic() - started
        for observer in observers:
            observer.record(receipt['status'], elapsed, receipt['headers'])

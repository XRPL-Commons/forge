"""Exact UTC ordering for GitHub timestamps and explicit data migrations."""
from datetime import datetime, timezone
import re

from .models import TransitionError

# GitHub RFC 3339 timestamps, with at most Python/SQLite's chosen microsecond
# precision. Reject additional precision instead of silently changing ordering.
_FORMAT = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})')


class RemoteTimestampError(TransitionError):
    code = 'remote_timestamp_invalid'


def instant(value):
    if not isinstance(value, str) or not _FORMAT.fullmatch(value):
        raise RemoteTimestampError('GitHub observation needs a valid timezone-aware timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise RemoteTimestampError('GitHub observation timestamp is invalid') from error


def normalize(value):
    return instant(value).isoformat(timespec='microseconds')


def order(value):
    """Missing observations sort before observed instants; invalid ones fail."""
    return datetime.min.replace(tzinfo=timezone.utc) if value is None else instant(value)


def register(connection):
    # Invoked only by explicit migration statements, never by triggers/views:
    # trusted_schema remains OFF and no Python function is marked innocuous.
    connection.create_function('forge_remote_time', 1, normalize, deterministic=True)

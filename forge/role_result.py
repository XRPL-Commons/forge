"""Strict, revision-bound results embedded in authoritative GitHub reviews.

This parser establishes format and optional identity equality only. Callers must
still verify review authorship, repository, current PR stage, and worker identity.
An invalid marker raises ValueError so callers cannot reinterpret it as prose.
"""
import json
import re
import uuid


_FIELDS = frozenset({'schema', 'role', 'outcome', 'head', 'base', 'assignment_id'})
_MARKER = re.compile(r'\bFORGE_RESULT\b')
_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_role_result_key')
        result[key] = value
    return result


def parse_role_result(body, *, expected_head=None, expected_base=None,
                      expected_assignment_id=None, expected_role=None):
    """Return one exact FORGE_RESULT record, None if absent, or raise ValueError.

    A marker must occupy one unindented line beginning ``FORGE_RESULT: ``.
    Multiple markers are invalid even when identical. Optional expected values
    bind the result to the caller's independently verified assignment/revision.
    """
    if not isinstance(body, str):
        raise ValueError('invalid_review_body')
    lines = [line for line in body.splitlines() if _MARKER.search(line)]
    if not lines:
        return None
    if len(lines) != 1:
        raise ValueError('multiple_role_result_markers')
    line = lines[0]
    if not line.startswith('FORGE_RESULT: ') or len(line) > 4096:
        raise ValueError('invalid_role_result_marker')
    try:
        record = json.loads(line[len('FORGE_RESULT: '):], object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid_role_result_json') from exc
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError('invalid_role_result_fields')
    if type(record['schema']) is not int or record['schema'] != 1:
        raise ValueError('unsupported_role_result_schema')
    if not isinstance(record['role'], str) or record['role'] not in {'review', 'audit'}:
        raise ValueError('invalid_role_result_role')
    if not isinstance(record['outcome'], str) or record['outcome'] not in {'pass', 'fail'}:
        raise ValueError('invalid_role_result_outcome')
    for name in ('head', 'base'):
        if not isinstance(record[name], str) or not _SHA.fullmatch(record[name]):
            raise ValueError('invalid_role_result_' + name)
    assignment = record['assignment_id']
    try:
        if not isinstance(assignment, str) or str(uuid.UUID(assignment)) != assignment:
            raise ValueError('invalid_role_result_assignment')
    except (ValueError, AttributeError) as exc:
        raise ValueError('invalid_role_result_assignment') from exc
    expected = {'head': expected_head, 'base': expected_base,
                'assignment_id': expected_assignment_id, 'role': expected_role}
    for name, value in expected.items():
        if value is not None and record[name] != value:
            raise ValueError('role_result_' + name + '_mismatch')
    return record

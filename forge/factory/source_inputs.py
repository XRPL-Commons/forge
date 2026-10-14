"""Immutable, credential-free source identities shared by mirror and publisher.

The host constructs these from the reserved assignment and its observations.
Worker files never authorize a new repository, revision or publication target.
"""
from copy import deepcopy
import hashlib
import json
import re

MAX_NOTES_BYTES = 1024 * 1024
FIELDS = frozenset({'version', 'repository_id', 'canonical_repository', 'object_format',
                    'base_oid', 'input_oids', 'manifest_digest', 'dependency_policy_digest', 'notes_digest'})
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}\Z')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate(value):
    if not isinstance(value, dict) or set(value) != FIELDS or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Unsupported immutable source input')
    if type(value['repository_id']) is not int or value['repository_id'] <= 0:
        raise ValueError('Source input requires a numeric repository identity')
    name = value['canonical_repository']
    if not isinstance(name, str) or not REPOSITORY.fullmatch(name) or name.split('/')[1] in {'.', '..'}:
        raise ValueError('Source input requires a canonical repository name')
    length = {'sha1': 40, 'sha256': 64}.get(value['object_format'])
    if length is None:
        raise ValueError('Unsupported Git object format')
    oid = re.compile(r'[0-9a-f]{' + str(length) + r'}\Z')
    if not isinstance(value['base_oid'], str) or not oid.fullmatch(value['base_oid']):
        raise ValueError('Source input requires an exact base commit')
    inputs = value['input_oids']
    if (not isinstance(inputs, list) or len(inputs) > 100
            or any(not isinstance(item, str) or not oid.fullmatch(item) for item in inputs)
            or inputs != sorted(set(inputs))):
        raise ValueError('Source inputs must be distinct sorted commit identities')
    for key in ('manifest_digest', 'dependency_policy_digest', 'notes_digest'):
        if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
            raise ValueError('Source input requires immutable content digests')
    return deepcopy(value)


def create(*, repository_id, canonical_repository, base_oid, input_oids,
           manifest, dependency_policy_digest, notes, object_format='sha1'):
    """Freeze already-authorized host inputs; this function grants no access."""
    if not isinstance(manifest, dict) or not manifest or not isinstance(notes, dict):
        raise ValueError('Source input requires an assignment manifest and notes')
    if len(encoded(notes)) > MAX_NOTES_BYTES:
        raise ValueError('Assignment notes exceed their byte bound')
    return validate(dict(version=1, repository_id=repository_id,
        canonical_repository=canonical_repository, object_format=object_format,
        base_oid=base_oid, input_oids=sorted(set(input_oids)), manifest_digest=digest(manifest),
        dependency_policy_digest=dependency_policy_digest, notes_digest=digest(notes)))


def assignment(source_input, notes, *, manifest):
    """Return the read-only worker document after checking both retained inputs."""
    source = validate(source_input)
    if (not isinstance(notes, dict) or len(encoded(notes)) > MAX_NOTES_BYTES
            or digest(notes) != source['notes_digest'] or digest(manifest) != source['manifest_digest']):
        raise ValueError('Assignment content differs from its pinned source input')
    if 'source_input' in notes:
        raise ValueError('Source input is supplied by the host, outside note content')
    return {**deepcopy(notes), 'source_input': source}

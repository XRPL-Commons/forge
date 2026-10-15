"""Trusted Tailscale Serve operator identity, linked to a GitHub operator.

Tailscale Serve adds ``Tailscale-User-Login`` to requests from tailnet users
and never for tagged devices. Only the operator UI proxy may turn that into
``X-Forge-Tailscale-User-Login``, and only for a request it received from the
local Serve upstream; the status API honors it only behind the UI's upstream
secret. Every other path strips it.

A tailnet login is linked to an already authorized GitHub operator, so actor
IDs (``github:<id>``), audit rows, approvals and agent grants keep one stable
identity across sign-in methods. The link is recorded when that operator first
signs in with GitHub through Serve (bootstrap records the first operator), and
an operator can unlink it, which revokes its Tailscale sessions immediately.
"""
import re

DOMAIN = 'operator-identity.json'
# Proxy-to-API header: set by the UI proxy only, never forwarded from a client.
UPSTREAM_HEADER = 'X-Forge-Tailscale-User-Login'
MODES = ('github', 'tailscale')
DEFAULT = {'version': 1, 'mode': 'github', 'tailnet_operators': []}
LOGIN = re.compile(r'[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)*')
# Tailscale reports tagged nodes under this pseudo-user; a tag is not a person.
TAGGED = frozenset({'tagged-devices', 'tagged-devices@'})
NONCE = re.compile(r'[A-Za-z0-9_-]{43}')


def login(value):
    """A normalized person login from the trusted header, or None."""
    if not isinstance(value, str) or len(value) > 320:
        return None
    value = value.strip().lower()
    if not value or value in TAGGED or value.startswith('tagged-devices') or not LOGIN.fullmatch(value):
        return None
    return value


def validate(value):
    if (not isinstance(value, dict) or set(value) != set(DEFAULT) or value['version'] != 1
            or value['mode'] not in MODES or not isinstance(value['tailnet_operators'], list)
            or len(value['tailnet_operators']) > 100):
        raise ValueError('Operator identity needs a mode and a bounded list of linked tailnet logins')
    seen = set()
    for row in value['tailnet_operators']:
        if (not isinstance(row, dict) or set(row) != {'login', 'github_id', 'nonce', 'linked_at'}
                or login(row['login']) != row['login'] or row['login'] in seen
                or type(row['github_id']) is not int or row['github_id'] <= 0
                or not isinstance(row['nonce'], str) or not NONCE.fullmatch(row['nonce'])
                or type(row['linked_at']) not in (int, float)):
            raise ValueError('Each linked tailnet login needs its GitHub operator ID')
        seen.add(row['login'])
    return value

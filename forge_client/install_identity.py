"""Observe the laptop's tailnet user; never infer it from a GitHub login."""
import json
from urllib.request import Request, build_opener, ProxyHandler

from .contracts import require
from .connection import NoRedirect
from .github import run_bounded
from .tailnet import executable as tailscale_executable, environment as tailscale_environment


def operator(github, *, run=run_bounded):
    status = json.loads(run([tailscale_executable(), 'status', '--json'], timeout=15, environ=tailscale_environment()))
    own = status.get('Self') or {}
    user = (status.get('User') or {}).get(str(own.get('UserID'))) or {}
    name = user.get('LoginName')
    require(status.get('BackendState') == 'Running' and not own.get('Tags')
            and isinstance(name, str) and '@' in name and not name.startswith('tagged-devices'),
            'tailnet_user_required', 'Connect this computer to Tailscale as the installing operator.')
    return {'operator_id': github.identity()['id'], 'login': name.strip().lower()}


def configure(factory, *, apply=False):
    linked = operator(factory.github)
    result = factory.call('identity', tailnet_operator=linked, apply=apply)
    if result.get('status') == 'identity_ready':
        # This independent request also proves the laptop, proxy and API agree on
        # the installing operator. The server's own tailnet identity is different.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(result['origin'] + '/api/auth/session',
                                 headers={'Accept': 'application/json'}), timeout=20) as response:
            raw = response.read(4097)
            require(response.status == 200 and len(raw) <= 4096, 'identity_unverified')
            value = json.loads(raw)
        require(value.get('ok') is True and value.get('enabled') is True
                and (value.get('operator') or {}).get('id') == linked['operator_id'],
                'identity_unverified', 'HTTPS did not confirm your Tailscale operator identity; resume after checking private-network access.')
    return result

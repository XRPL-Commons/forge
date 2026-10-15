"""SSH-authorized linking of the installing operator's observed tailnet identity."""
import json

from forge_client.contracts import fields, require
from .operator_auth import OperatorAuth
from .operator_bootstrap import Bootstrap
from .tailnet_identity import login


def configure(root, *, setup, tailnet_operator, actor_id, apply=False):
    fields(tailnet_operator, ('operator_id', 'login'))
    require(type(apply) is bool and tailnet_operator['operator_id'] == actor_id
            and login(tailnet_operator['login']) == tailnet_operator['login'], 'invalid_tailnet_operator')
    auth = OperatorAuth(root)
    config = auth.config()
    require(config is not None and actor_id in config['operators'], 'operator_required')
    bootstrap = Bootstrap(root)
    target = {key: config[key] for key in ('origin', 'client_id', 'operators')}
    view = bootstrap.snapshot(target)
    settings = auth.identity_settings()
    linked = any(row['github_id'] == actor_id and row['login'] == tailnet_operator['login']
                 for row in settings['tailnet_operators'])
    serve = json.loads(bootstrap.command(['tailscale', 'serve', 'status', '--json']))
    units = bootstrap.command(['systemctl', '--user', 'cat', 'forge-ui.service']).decode()
    proxy = bool(serve) and 'NITRO_FORGE_TAILSCALE_IDENTITY=serve' in units
    if linked and settings['mode'] == 'tailscale' and proxy:
        return {'status': 'identity_ready', 'origin': config['origin'], 'operator_id': actor_id}
    if not apply:
        return {'status': 'identity_required', 'origin': config['origin'], 'operator_id': actor_id}
    if not proxy:
        bootstrap.execute({'version': 1, 'action': 'resume', 'target': target,
                           'revision': view['revision'], 'secret': None})
    auth.link_tailnet(tailnet_operator['login'], actor_id)
    auth.set_identity_mode('tailscale')
    return configure(root, setup=setup, tailnet_operator=tailnet_operator, actor_id=actor_id)

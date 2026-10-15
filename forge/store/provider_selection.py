"""Select a verified coding account before pinning an auxiliary assignment."""
from .executions import provider_available


def select_provider(db, config, now):
    pool = config.get('provider_pool') or [dict(id=None, **{key: config.get(key) for key in
        ('provider', 'model', 'provider_environment', 'provider_auth_file_env')})]
    ready = config.get('provider_ready')
    for entry in pool:
        if ready is not None and entry['id'] not in ready:
            continue
        selected = {**config, **{key: value for key, value in entry.items() if key != 'id'}}
        if provider_available(db, entry['provider'], entry['id'], selected, now):
            return entry, selected
    return None, None

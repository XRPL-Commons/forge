"""Pinned signing qualification for the installation's disposable probe repository."""
import time

from forge_client.contracts import require
from .publication_qualification import Access, Qualification, QualificationError
from .request_metrics import request_scope


def qualify(root, *, setup, repository, probe_repository, request_id, apply):
    from forge_client.contracts import repository as target
    from .store.runtime import required
    chosen, probe = target(repository), target(probe_repository)
    require(type(apply) is bool and chosen['id'] != probe['id'], 'invalid_publication_probe')
    from .settings import Settings
    from .store.configuration import Configuration
    from .factory.publisher_runtime import SigningCapabilities
    from .factory.github import GitHub
    settings = Settings(root, scope='active')
    authorized = (setup.read().get('installation_consent') or {}).get('binding', {}).get('repositories', [])
    require(all(item in authorized for item in (chosen, probe)), 'publication_repository_changed')
    rows = [row for row in settings.view()['repositories'] if row['access_ready']]
    require(all(any(row['id'] == item['id'] and row['name'] == item['name'] for row in rows)
                for item in (chosen, probe)), 'publication_repository_changed')
    access = Access(settings, installation_targets=(chosen,))
    try:
        with request_scope(until=time.monotonic() + 200):
            store = required(root)
            previous = Configuration(store).get('publication_signing_probe', request_id)
            if previous and previous['settings'].get('stage') == 'qualified':
                saved = previous['settings']
                require(saved['repository_id'] == chosen['id'] and saved['repository'] == chosen['name']
                        and saved['probe_repository_id'] == probe['id'] and saved['probe_repository'] == probe['name'],
                        'publication_repository_changed')
                capability = SigningCapabilities(store, GitHub(settings, {}))(
                    {'repo_id': chosen['id'], 'repo': chosen['name'], 'target_branch': saved['target_branch']})
                if capability['qualified']:
                    return {'status': 'qualified', 'receipt_id': request_id, **capability}
            return Qualification(store, access).execute(chosen['name'], probe['name'],
                apply=apply, request_id=request_id)
    except QualificationError as error:
        return {'status': 'blocked', 'reason': str(error)}
    finally:
        with request_scope(until=time.monotonic() + 30):
            access.close()

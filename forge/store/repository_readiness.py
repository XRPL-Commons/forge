"""Identity of the persisted facts used by repository admission.

The evaluator reads these in one snapshot. The lifecycle writer compares the
identity in its transaction before applying that observation to a delivery.
"""
from .transitions import manifest_hash


def read_revision(db, *, domain, scope, repository_id):
    def rows(sql, args=()):
        return [dict(row) for row in db.execute(sql, args)]
    return manifest_hash({
        'configuration': rows("SELECT revision FROM configuration WHERE domain=? AND subject='instance' AND active=1", (domain,)),
        'setup': rows("SELECT generation FROM setup_instances WHERE scope='live'"),
        'controls': rows('SELECT name,enabled,generation,details_json FROM runtime_controls WHERE name IN (?,?,?,?) ORDER BY name',
                         ('factory_paused', 'factory_active', 'onboarding_active', scope + ':access')),
        'roles': rows('SELECT * FROM access_role_observations WHERE scope=? ORDER BY role', (scope,)),
        'access': rows('SELECT * FROM access_repository_observations WHERE scope=? AND repository_id=? ORDER BY role',
                       (scope, repository_id)),
        'protection': rows('SELECT * FROM repository_setup_observations WHERE repository_id=?', (repository_id,)),
    })

"""Service-account half of a clean Forge reinstall: observe and drain only."""
import argparse
import json
from pathlib import Path

from .host_update import Record, SERVICES, source_revision
from .onboarding import Onboarding
from .store.runtime import required
from forge_client.contracts import digest, require


def inspect(root):
    store = required(root)
    setup = Onboarding(root)
    with store.read_snapshot() as db:
        data, setup_revision = setup.store._read(db)
        require(data, 'setup_required')
        configuration = [tuple(row) for row in db.execute(
            'SELECT domain,subject,revision FROM configuration WHERE active=1 ORDER BY domain,subject')]
        identity = [tuple(row) for row in db.execute('SELECT origin,client_id,credential_ref,credential_digest,source_epoch,authorization_epoch FROM operator_auth_configuration')]
        setup_fingerprint = digest({'setup_revision': setup_revision, 'configuration': configuration, 'identity': identity})
        pending = db.execute("SELECT COUNT(*) FROM external_effects WHERE status IN ('pending','running','uncertain')").fetchone()[0]
        leases = db.execute('SELECT (SELECT COUNT(*) FROM job_leases)+(SELECT COUNT(*) FROM auxiliary_leases)+(SELECT COUNT(*) FROM discussion_leases)').fetchone()[0]
    managed = Record(root).data.get('managed', {})
    units = Path.home() / '.config/systemd/user'
    expected = [units / (name + '.service') for name in SERVICES]
    require(all(str(path) in managed for path in expected), 'unsafe_reset', 'Managed service ownership is incomplete.')
    return {'instance': store.instance_id, 'generation': data['generation'], 'phase': data['phase'],
            'source_revision': source_revision(), 'setup_revision': setup_revision, 'setup_fingerprint': setup_fingerprint, 'units': {str(path): managed[str(path)] for path in expected},
            'pending_effects': pending, 'leases': leases, 'reset_receipt': data.get('reset_receipt'),
            'error': data.get('error')}


def drain(root, instance, generation):
    store = required(root)
    require(store.instance_id == instance, 'setup_changed')
    setup = Onboarding(root)
    data = setup.store.read()
    receipt = data.get('reset_receipt') or {}
    if receipt.get('generation') != generation:
        require(data['generation'] == generation, 'setup_changed')
        setup.queue('reset', setup.revision(data), generation=generation)
    return inspect(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--instance')
    parser.add_argument('--generation')
    args = parser.parse_args()
    result = drain(args.root, args.instance, args.generation) if args.instance else inspect(args.root)
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()

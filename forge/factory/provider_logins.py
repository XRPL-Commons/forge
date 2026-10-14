"""Local, account-scoped login admission; never contacts the provider."""
import hashlib
import json
from pathlib import Path
import time

from ..credentials import Credentials
from ..store.runtime import writable
from .codex_auth import renewable


def check_logins(root, store, config, *, clock=time.time):
    references = sorted({entry['provider_auth_file_env']
        for entry in config.get('provider_pool') or [config]
        if entry.get('provider') == 'codex' and entry.get('provider_auth_file_env')})
    if not references:
        return []
    credentials = Credentials(root)
    observations = []
    # A reconnect must not race a stale read followed by a new auth block.
    with credentials.locked():
        for reference in references:
            try:
                path = credentials.environment(only=[reference]).get(reference)
                problem = renewable(json.loads(Path(path).read_text())) if path else 'missing'
            except (OSError, ValueError):
                problem = 'unreadable'
            observations.append((reference, problem))
        now = clock()
        with store.write() as db:
            writable(db)
            for reference, problem in observations:
                identifier = 'login-check:' + hashlib.sha256(reference.encode()).hexdigest()
                prior = db.execute('SELECT details_json FROM provider_observations WHERE id=?',
                                   (identifier,)).fetchone()
                details = json.loads(prior[0]) if prior else {}
                cooldown = db.execute('SELECT * FROM provider_cooldowns WHERE account_ref=?',
                                      (reference,)).fetchone()
                owned = bool(cooldown and details.get('cooldown') == dict(cooldown))
                if not problem:
                    # Remove our observation, exposing any independent provider
                    # observation again. Never publish an overriding ready row.
                    db.execute('DELETE FROM provider_observations WHERE id=?', (identifier,))
                    if owned:
                        db.execute('DELETE FROM provider_cooldowns WHERE account_ref=?', (reference,))
                    continue
                details = {'source': 'login_check', 'problem': 'login_' + problem}
                if owned or cooldown is None or cooldown['retry_at'] <= now:
                    db.execute('INSERT INTO provider_cooldowns(account_ref,provider,reason_code,retry_at,observed_at,source_job_id) '
                        "VALUES(?,'codex','blocked_auth',?,?,NULL) ON CONFLICT(account_ref) DO UPDATE SET "
                        'provider=excluded.provider,reason_code=excluded.reason_code,retry_at=excluded.retry_at,'
                        'observed_at=excluded.observed_at,source_job_id=NULL',
                        (reference, now + config['availability_retry_seconds'], now))
                    details['cooldown'] = dict(db.execute('SELECT * FROM provider_cooldowns WHERE account_ref=?',
                                                         (reference,)).fetchone())
                db.execute('INSERT INTO provider_observations(id,provider,account_ref,readiness,reason_code,observed_at,details_json) '
                    "VALUES(?,'codex',?,'unavailable','blocked_auth',?,?) ON CONFLICT(id) DO UPDATE SET "
                    'observed_at=excluded.observed_at,details_json=excluded.details_json',
                    (identifier, reference, now, json.dumps(details, sort_keys=True)))
    return [{'account_ref': reference, 'reason': 'login_' + problem} for reference, problem in observations if problem]

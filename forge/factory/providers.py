"""Availability is independent of issue quality and role capacity."""


def available(entry, data, now, *, codex_parallel_auth=False):
    if data.get('provider_cooldowns', {}).get(entry['provider'], {}).get('until', 0) > now:
        return False
    reference = entry.get('provider_auth_file_env')
    if reference and entry['provider'] == 'codex' and codex_parallel_auth:
        # Drain legacy refresh writers before sharing access-only snapshots.
        return not any(not job['cleanup']
                       and (job.get('runner_config') or {}).get('provider_auth_file_env') == reference
                       and not (job.get('runner_config') or {}).get('codex_parallel_auth', False)
                       for job in data['jobs'].values())
    # A rotating OAuth refresh token has one writer. API-key jobs can run in
    # parallel. Release this lease only after credentials are saved and teardown
    # is verified; it therefore survives controller crashes.
    return not reference or not any(
        not job['cleanup'] and (job.get('runner_config') or {}).get('provider_auth_file_env') == reference
        for job in data['jobs'].values())

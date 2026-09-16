"""Availability is independent of issue quality and role capacity."""


def available(entry, data, now):
    if data.get('provider_cooldowns', {}).get(entry['provider'], {}).get('until', 0) > now:
        return False
    reference = entry.get('provider_auth_file_env')
    # A rotating OAuth refresh token has one writer. API-key jobs can run in
    # parallel. Release this lease only after credentials are saved and teardown
    # is verified; it therefore survives controller crashes.
    return not reference or not any(
        not job['cleanup'] and (job.get('runner_config') or {}).get('provider_auth_file_env') == reference
        for job in data['jobs'].values())

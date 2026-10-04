"""Coding-account health: one plain row per provider in the pool.

A cooldown is recorded when a worker job reports an expired sign-in
(`blocked_auth`) or an exhausted quota (`blocked_quota`). Its `until` only
spaces out retries: once it lapses Forge retries, and a failed retry renews
it. So a lapsed cooldown still describes the account until an attempt on
that provider gets past sign-in, or the operator reconnects it (which
removes the cooldown).
"""

STATUS = {'blocked_auth': 'sign_in_expired', 'blocked_quota': 'quota'}
COOLDOWN_REASONS = tuple(STATUS)
# Outcomes a job reaches only after its agent ran, i.e. after sign-in worked.
RAN = ('accepted', 'investigated', 'failed', 'needs_decision', 'blocked_environment')


def streak_start(data, provider, reason):
    """When the current run of `reason` waits on this provider began, or None."""
    start = None
    for event in reversed(data.get('events', [])):
        if event.get('provider') != provider:
            continue
        if event.get('kind') != 'availability_wait' or event.get('reason') != reason:
            break
        start = event.get('at')
    return start


def _attempt_start(data, entry):
    job = data.get('jobs', {}).get(entry.get('attempt')) or {}
    return job.get('created_at')


def _recovered(data, provider, entry):
    """True when an attempt on this provider ran after the cooldown began."""
    start = entry.get('since') or _attempt_start(data, entry)
    if start is None:
        return True
    return any(job.get('provider') == provider and job.get('outcome') in RAN and (job.get('created_at') or 0) > start
               for job in data.get('jobs', {}).values())


def waiting_issues(data, provider):
    """Issues whose latest attempt is waiting on this provider's account."""
    latest = {}
    for job in data.get('jobs', {}).values():
        key = job.get('issue_key')
        if key and (key not in latest or (job.get('created_at') or 0) >= (latest[key].get('created_at') or 0)):
            latest[key] = job
    count = 0
    for key, issue in data.get('issues', {}).items():
        job = latest.get(key)
        if (issue.get('state') not in ('merged', 'cancelled') and not issue.get('active')
                and issue.get('reason') in COOLDOWN_REASONS and job
                and job.get('provider') == provider and job.get('outcome') == 'waiting'):
            count += 1
    return count


def current_cooldown(data, provider, now):
    """The provider's cooldown while it still describes the account, else None."""
    entry = (data.get('provider_cooldowns') or {}).get(provider)
    if not entry or entry.get('reason') not in COOLDOWN_REASONS:
        return None
    if entry.get('until', 0) > now:
        return entry
    if _recovered(data, provider, entry):
        return None
    # A login does not come back by itself; a quota does, so a lapsed quota
    # cooldown only counts while work still waits on it or a retry runs.
    if entry['reason'] == 'blocked_auth':
        return entry
    retrying = any(job.get('provider') == provider and job.get('outcome') is None for job in data.get('jobs', {}).values())
    return entry if retrying or waiting_issues(data, provider) else None


def cooldown_since(data, provider, reason, now):
    """The first-seen time to store when renewing a cooldown for `reason`.

    Repeated failures for the same reason keep the original time, so the
    operator reads "since 22:25" rather than the latest retry."""
    previous = current_cooldown(data, provider, now)
    if previous and previous.get('reason') == reason:
        return previous.get('since') or streak_start(data, provider, reason) or now
    return now


def health(data, runtime, now, names=None):
    """One row per provider in the pool, in pool order."""
    names = names or {}
    pool = runtime.get('provider_pool') or []
    ready = runtime.get('provider_ready')
    rows, seen = [], set()
    for entry in pool:
        provider = entry['provider']
        if provider in seen:
            continue
        seen.add(provider)
        entries = [e for e in pool if e['provider'] == provider]
        cooldown = current_cooldown(data, provider, now)
        verified = ready is None or any(e['id'] in ready for e in entries)
        detail = None
        if cooldown:
            # Any `blocked_auth` is a sign-in problem; `detail` says which one
            # (e.g. `login_access_only`: the saved login can no longer renew).
            status, reason, detail = STATUS[cooldown['reason']], cooldown['reason'], cooldown.get('detail')
            since = cooldown.get('since') or streak_start(data, provider, reason) or _attempt_start(data, cooldown)
            until = cooldown.get('until')
        elif not verified:
            status, reason, since, until = 'unavailable', 'verification_required', None, None
        else:
            status, reason, since, until = 'ok', None, None, None
        rows.append({'provider': provider, 'name': names.get(provider) or provider.capitalize(),
                     'ready': status == 'ok', 'status': status, 'since': since, 'until': until,
                     'reason_code': reason, 'detail': detail, 'waiting_issues': waiting_issues(data, provider) if status != 'ok' else 0})
    return rows


def status_label(cooldown):
    """The Coding accounts page's per-model wording for an active cooldown."""
    reason = (cooldown or {}).get('reason')
    if reason == 'blocked_auth':
        return 'sign in again'
    if reason == 'blocked_quota':
        return 'usage limit; retry pending'
    return 'unavailable; retry pending'

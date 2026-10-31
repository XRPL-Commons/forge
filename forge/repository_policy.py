"""Readiness shared by setup and the controller's fresh merge checks."""


def protection_ready(report):
    # Hidden bypass details are not evidence that a bypass is missing. Keep the
    # explicit operator-review warning; never certify the full bypass list.
    return bool(report and report.get('available') and report.get('requirements')
                and all(row['passed'] for row in report['requirements'])
                and (report.get('bypass') or {}).get('status') != 'missing')


def github_protection(config, repo_id):
    """External enforcement is explicit per repository; Forge gates are separate."""
    row = next((r for r in config['repositories'] if r['id'] == repo_id), {})
    return row.get('github_protection', 'required')


def plan_unavailable(error):
    return error.status == 403 and getattr(error, 'plan_required', False) is True

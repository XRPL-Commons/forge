"""Readiness shared by setup and the controller's fresh merge checks."""


def protection_ready(report):
    # External rules are observations, not a Forge-managed minimum policy.
    # Legacy baseline/bypass verdicts do not govern supervisor admission.
    return bool(report and report.get('available') and not report.get('errors')
                and all(row['passed'] for row in report.get('requirements', []) if row.get('id') == 'readable'))


def plan_unavailable(error):
    return error.status == 403 and getattr(error, 'plan_required', False) is True

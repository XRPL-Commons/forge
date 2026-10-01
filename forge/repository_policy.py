"""Readiness shared by setup and the controller's fresh merge checks."""


def protection_ready(report):
    # Hidden bypass details are not evidence that a bypass is missing. Keep the
    # explicit operator-review warning; never certify the full bypass list.
    return bool(report and report.get('available') and report.get('requirements')
                and all(row['passed'] for row in report['requirements'])
                and (report.get('bypass') or {}).get('status') != 'missing')

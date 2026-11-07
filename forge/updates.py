"""Read-only discovery of published Forge builds; never installs or fetches Git refs."""
import http.client
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.request

from .app_access import AppClient, AccessError
from .release_artifacts import ASSETS, REPOSITORY, artifact_kinds, asset_url, manifest_shape
from .request_metrics import request_scope, request_attempt, RequestBudgetExhausted

SOURCE = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[a-f0-9]{40}')
RELEASES = 'https://github.com/' + REPOSITORY + '/releases'


def installed_revision():
    try:
        result = subprocess.run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=5, check=True)
        value = result.stdout.strip()
        return value if SHA.fullmatch(value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def public_manifest(revision):
    """Validate the public release envelope without operator credentials or cache writes."""
    with request_attempt() as (timeout, receipt):
        with urllib.request.urlopen(asset_url(revision, 'manifest.json'), timeout=timeout) as response:
            receipt.update(status=response.status, headers=response.headers)
            raw = response.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError('Release manifest exceeded the size limit')
    return json.loads(raw)


class UpdateCheck:
    def __init__(self, request=None, clock=time.time, monotonic=time.monotonic, fetch_manifest=None):
        self.request = request or AppClient().request
        self.fetch_manifest = fetch_manifest or public_manifest
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.Lock()
        self.cached = None
        self.checked = 0

    def view(self, installed, *, refresh=False):
        with self.lock:
            now = self.monotonic()
            # Share one check across tabs/operators. Manual checks have a one-minute cooldown.
            age = now - self.checked
            if self.cached and self.cached['installed_revision'] == installed and age < (60 if refresh else 3600):
                return dict(self.cached)
            result = dict(ok=True, installed_revision=installed, latest=None,
                          status='unavailable', checked_at=self.clock(), error=None, releases_url=RELEASES)
            try:
                with request_scope(until=time.monotonic() + 10):
                    self._check(result)
            except (AccessError, RequestBudgetExhausted, ValueError, TypeError, KeyError, OSError, http.client.HTTPException) as error:
                result['error'] = ('GitHub releases are not publicly accessible yet.'
                                   if isinstance(error, AccessError) and error.status == 404 else
                                   'Could not check GitHub. Try again later.')
            self.cached, self.checked = result, now
            return dict(result)

    def _check(self, result):
        releases = self.request('GET', '/repos/' + REPOSITORY + '/releases?per_page=100', None)
        if not isinstance(releases, list):
            raise ValueError('Invalid release list')
        candidates = []
        for release in releases:
            tag = release.get('tag_name', '')
            revision = tag.removeprefix('forge-build-')
            if (release.get('draft') or release.get('prerelease') or not tag.startswith('forge-build-')
                    or not SHA.fullmatch(revision) or not release.get('published_at')):
                continue
            assets = {item['name'] for item in release.get('assets', [])}
            # Both delivery versions include the UI and a manifest; verify the
            # selected release's declared artifact set below before offering it.
            if not {ASSETS['ui'], 'manifest.json'} <= assets:
                continue
            candidates.append((release['published_at'], revision, tag, assets))
        if not candidates:
            result['status'] = 'no_release'
            return
        published, revision, tag, assets = max(candidates, key=lambda row: row[:3])
        manifest = manifest_shape(self.fetch_manifest(revision), revision)
        if not {ASSETS[kind] for kind in artifact_kinds(manifest)} <= assets:
            raise ValueError('Release is missing declared artifacts')
        result['latest'] = dict(revision=revision, published_at=published, url=RELEASES + '/tag/' + tag)
        installed = result['installed_revision']
        if installed == revision:
            result['status'] = 'current'
        elif installed is None:
            result['status'] = 'unknown'
        else:
            try:
                comparison = self.request('GET', '/repos/' + REPOSITORY + '/compare/' + installed + '...' + revision, None)
            except AccessError as error:
                if error.status != 404:
                    raise
                result['status'] = 'unknown'
                return
            result['status'] = {'ahead': 'available', 'behind': 'ahead', 'identical': 'current',
                                'diverged': 'diverged'}.get(comparison.get('status'), 'unknown')


checks = UpdateCheck()


def view(*, refresh=False):
    return checks.view(installed_revision(), refresh=refresh)

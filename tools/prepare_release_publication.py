#!/usr/bin/env python3
"""Allow publication of an exact tested revision, recovering only our own draft."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge.release_artifacts import REPOSITORY, release_tag


def prepare(revision, *, run=subprocess.run):
    tag = release_tag(revision)
    result = run(['gh', 'api', '--hostname', 'github.com',
                  'repos/' + REPOSITORY + '/releases/tags/' + tag],
                 capture_output=True, text=True)
    try:
        release = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('Could not inspect the prepared release; no publication state was changed.') from None
    if result.returncode:
        if isinstance(release, dict) and str(release.get('status')) == '404':
            return 'build'
        raise ValueError('GitHub release lookup failed; no publication state was changed.')
    if not isinstance(release, dict) or release.get('tag_name') != tag:
        raise ValueError('GitHub returned a different release; no publication state was changed.')
    if release.get('draft') is False:
        return 'published'
    if (release.get('draft') is not True or release.get('target_commitish') != revision
            or not isinstance(release.get('author'), dict)
            or release['author'].get('login') != 'github-actions[bot]'):
        raise ValueError('Only this workflow’s unpublished draft for the exact commit can be retried.')
    # A failed upload can leave a partial draft. Rebuild all assets together,
    # because rebuilding pinned source can still produce different image IDs.
    # Keep the tag. Published releases and drafts owned by humans are untouched.
    run(['gh', 'release', 'delete', tag, '--repo', REPOSITORY, '--yes'],
        check=True, capture_output=True, text=True)
    return 'build'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('revision')
    print(prepare(parser.parse_args().revision))

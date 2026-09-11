"""Read-only reconciliation and compatibility bridge. No agent or GitHub writes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from .config import Repository, load_repositories
from .github import GitHub


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot_path(state, repo):
    return Path(state) / "snapshots" / (hashlib.sha256(repo.encode()).hexdigest() + ".json")


def observe(repository, state, github=None):
    from .settings import Settings
    access = Settings(Path(state).parent).admission(repository.repo)
    if not access['allowed']:
        # Preserve the last inventory and lifecycle history. This return value
        # blocks new scheduling without overwriting old evidence as empty data.
        return {'repo': repository.repo, 'enabled': repository.enabled,
                'status': 'access_blocked', 'observed_at': time.time(), 'reason': access['reason']}
    try:
        result = (github or GitHub(state=state)).snapshot(repository)
    except Exception as exc:
        result = {"repo": repository.repo, "enabled": repository.enabled, "status": "unknown",
                  "observed_at": time.time(), "reason": type(exc).__name__}
    atomic_json(snapshot_path(state, repository.repo), result)
    if (Path(state)/'workflow.enabled').is_file() and result.get('status') == 'ok':
        try:
            from .workflow_control import reconcile_repository
            from .workflow_lifecycle import reconcile as reconcile_releases
            reconcile_releases(state)
            reconcile_repository(state,repository.repo)
        except Exception:
            # Keep the valid GitHub snapshot but fail NEW admission closed.
            result = {**result, 'status':'workflow_unavailable', 'reason':'workflow_reconciliation_failed'}
    return result


def shadow(config, state):
    repositories = load_repositories(config)
    with ThreadPoolExecutor(max_workers=3) as pool:
        snapshots = list(pool.map(lambda repo: observe(repo, state), repositories))
    result = {"schema": 1, "mode": "shadow", "observed_at": time.time(), "repos": snapshots}
    atomic_json(Path(state) / "snapshot.json", result)
    return result


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("shadow")
    s.add_argument("--config", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--loop", type=int, default=0)
    b = sub.add_parser("snapshot")
    b.add_argument("--repo", required=True)
    b.add_argument("--owner", required=True)
    b.add_argument("--author", default="forge-bot")
    b.add_argument("--state", required=True)
    args = p.parse_args()
    Path(args.state).mkdir(parents=True, exist_ok=True)
    if args.command == "snapshot":
        value = observe(Repository(args.repo, args.owner, author=args.author), args.state)
        if value["status"] != "ok":
            print("? ? ? ? ? ? ?")
            return 1
        counts = value["counts"]
        print(" ".join(str(counts[k]) for k in ("ready", "review", "audit", "wip", "sentback", "limbo", "builder")))
        return 0
    with open(Path(args.state) / "shadow.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            value = shadow(args.config, args.state)
            if not args.loop:
                print(json.dumps(value, indent=2))
                return 0
            time.sleep(max(30, args.loop))


if __name__ == "__main__":
    raise SystemExit(main())

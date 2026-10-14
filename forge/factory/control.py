"""One controller lease shared by the setup supervisor and host commands; operator requests."""
import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import time

from ..settings import Settings
from ..store.files import StateError
from .config import load
from .github import GitHubUnavailable


class ControllerBusy(StateError):
    pass


@contextmanager
def controller_lock(root, *, timeout=0):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / 'state-supervisor.lock', 'a') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControllerBusy('Another supervisor owns this factory; drain it before cutover') from None
                time.sleep(min(0.2, remaining))
        yield


def queue(root, action, target, reason, *, actor_id="local:cli"):
    from .store_operator import selected_service
    return selected_service(root, actor_id=actor_id).stop_job(target, reason, action=action)


def request_check(root, key, clock=time.time, *, actor_id="local:cli"):
    """Ask the supervisor to observe one issue first in its next cycle, e.g. right after
    the operator approved on GitHub. Only reorders observation; changes no state."""
    from .store_operator import selected_service
    return selected_service(root, actor_id=actor_id).request_check(key)


def view(root):
    return public_views(root).factory()


def public_views(root):
    from ..store.runtime import required
    store = required(root)
    from .store_views import PipelineViews
    from .live_output import runner_tail
    return PipelineViews(store, repositories=lambda runtime: Settings(root).repository_views(runtime=runtime),
                         runtime=lambda: load(root), live=runner_tail(root))


def factory_view(root, **arguments):
    return public_views(root).factory(**arguments)


def save_runtime(root, config, revision):
    from .config import save
    save(root, config, revision=revision)
    return view(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verification-configure'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--target')
    parser.add_argument('--revision', help='The runtime_revision the change applies to; omit it to print the current one')
    parser.add_argument('--checks', help='JSON list of {name, app_id}; [] disables CI verification')
    args = parser.parse_args(argv)
    try:
        if not args.revision:
            # Read the revision to change; nothing is written without it.
            from ..settings import digest
            print(json.dumps({'runtime_revision': digest(load(Path(args.root)))}))
            return 2
        from .verification import configure
        print(json.dumps(configure(Path(args.root), args.target, json.loads(args.checks or 'null'), args.revision)))
        return 0
    except (ValueError, StateError, OSError, GitHubUnavailable) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())

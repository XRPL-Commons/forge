"""Read-only issue inventory over the selected store, separate from admission."""
from functools import cached_property
from pathlib import Path
import threading
import time

from ..settings import Settings
from ..store.runtime import required
from .config import load
from .github import GitHub


class IssueInventory:
    def __init__(self, root, *, settings=None, github=None, clock=time.time):
        self.root = Path(root)
        self.store = required(self.root)
        self.settings = settings or Settings(root)
        self.clock = clock
        if github is not None:
            self.github = github

    @cached_property
    def runtime(self):
        return load(self.root)

    @cached_property
    def github(self):
        return GitHub(self.settings, self.runtime)

    def refresh(self, force=False):
        from .store_inventory import StoreInventory
        return StoreInventory(self.store, self.settings, self.github, config=self.runtime, clock=self.clock).refresh(force)

    REFRESH_SPACING = 30
    _refresh_started = 0.0  # per process: every GET used to start a thread
    _refresh_lock = threading.Lock()

    def start_refresh(self, force=False):
        now = time.monotonic()
        with IssueInventory._refresh_lock:
            if not force and now - IssueInventory._refresh_started < self.REFRESH_SPACING:
                return
            IssueInventory._refresh_started = now
        threading.Thread(target=self.refresh, args=(force,), daemon=True).start()

    def view(self, *, repo_id=None, state='open', query='', page=1, key=None):
        from .store_views import PipelineViews
        return PipelineViews(self.store, repositories=lambda runtime: self.settings.repository_views(runtime=runtime),
                             runtime=lambda: load(self.root), clock=self.clock).issues(
            repo_id=repo_id, state=state, query=query, page=page, key=key)

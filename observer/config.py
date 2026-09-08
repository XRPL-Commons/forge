import os, re, glob, shlex, stat
from pathlib import Path
from observer.isolation import protected_path, IsolationError

WD = os.environ.get("FORGE_HOME", os.path.dirname(os.path.dirname(__file__)))
STATE_DIR = os.environ.get("FORGE_OBSERVER_STATE", os.path.join(WD, "state", "observer"))

_KV = re.compile(r'^\s*([A-Z_]+)="?([^"\n]*)"?\s*$')

class Repo:
    def __init__(self, name, repo, clone_build, enabled, observer_mirror=""):
        self.observer_mirror = observer_mirror
        self.name = name
        self.repo = repo
        self.clone_build = clone_build
        self.enabled = enabled
    def __eq__(self, o):
        return isinstance(o, Repo) and self.__dict__ == o.__dict__
    def __repr__(self):
        return f"Repo({self.name!r}, {self.repo!r}, enabled={self.enabled})"

def _parse(path):
    kv = {}
    allowed = {"REPO", "OWNER_LABEL", "CLONE_BUILD", "CLONE_REVIEW", "CLONE_AUDIT",
               "RECOVERY_AUTHOR", "ENABLED", "OBSERVER_MIRROR"}
    with open(path) as f:
        for line in f:
            fields = shlex.split(line, comments=True)
            if not fields:
                continue
            if len(fields) != 1 or "=" not in fields[0]:
                raise ValueError("invalid observer repository declaration")
            key, value = fields[0].split("=", 1)
            if key not in allowed or key in kv:
                raise ValueError("unknown or duplicate observer repository setting")
            kv[key] = value
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", kv.get("REPO", "")):
        raise ValueError("invalid observer repository identity")
    if kv.get("ENABLED", "false") not in {"true", "false"}:
        raise ValueError("enabled must be true or false")
    name = os.path.splitext(os.path.basename(path))[0]
    return Repo(name=name, repo=kv["REPO"], clone_build=kv.get("CLONE_BUILD", ""),
                enabled=kv.get("ENABLED", "false") == "true",
                observer_mirror=kv.get("OBSERVER_MIRROR", ""))


def load_repos(repos_d):
    repos = [_parse(p) for p in glob.glob(os.path.join(repos_d, "*.repo"))]
    active = [r for r in repos if r.enabled]
    if len({r.repo.lower() for r in active}) != len(active):
        raise ValueError("duplicate observer repository identity")
    mirrors = [os.path.realpath(r.observer_mirror) for r in active if r.observer_mirror]
    if len(set(mirrors)) != len(mirrors):
        raise ValueError("observer repositories must not share a mirror")
    return sorted(active, key=lambda r: r.name)

def resolve_claude():
    """Use only the explicitly pinned, root-provisioned observer executable.

    Deployment must verify interpreter/shared-library provenance before starting
    Python; this runtime check cannot retroactively establish code integrity.
    """
    configured = os.environ.get("FORGE_OBSERVER_CLAUDE_BIN", "")
    if not configured or not Path(configured).is_absolute():
        raise IsolationError("FORGE_OBSERVER_CLAUDE_BIN must name an explicit absolute executable")
    executable = protected_path(configured, directory=False)
    info = executable.stat()
    if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or not os.access(configured, os.X_OK):
        raise IsolationError("observer Claude executable must be a root-owned executable regular file")
    # A root-owned file inside an observer-owned directory can still be replaced.
    if any(parent.stat().st_uid != 0 for parent in executable.parents):
        raise IsolationError("observer Claude executable ancestors must be root-owned")
    return configured

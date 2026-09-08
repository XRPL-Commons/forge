import os, subprocess, tempfile, re
from pathlib import Path
from observer.isolation import protected_path, IsolationError

def _sha_path(name, state_dir):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
        raise ValueError("invalid checkpoint name")
    return os.path.join(state_dir, f"{name}.sha")

def read_sha(name, state_dir):
    p = _sha_path(name, state_dir)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        value = f.read().strip()
    if not value:
        return None
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("invalid checkpoint SHA")
    return value

def write_sha(name, sha, state_dir):
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("invalid checkpoint SHA")
    _sha_path(name, state_dir)
    os.makedirs(state_dir, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=state_dir, prefix=".checkpoint-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sha.strip() + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, _sha_path(name, state_dir))
        directory = os.open(state_dir, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)

class Commit:
    def __init__(self, sha, author, subject):
        self.sha = sha
        self.author = author
        self.subject = subject

def _git(clone, *args, check=True):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_CONFIG_") and key not in {
               "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SSH", "GIT_SSH_COMMAND",
               "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_SSH_COMMAND="/usr/bin/ssh -oBatchMode=yes -oIdentitiesOnly=yes -oIdentityAgent=none -oClearAllForwardings=yes -oPermitLocalCommand=no")
    return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", "-C", clone, *args], check=check,
                          capture_output=True, text=True, timeout=120, env=env)

def verify_mirror(clone, repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise IsolationError("invalid repository identity")
    mirror = protected_path(clone)
    protected_path(mirror / "config", directory=False)
    for name in ("objects", "refs"):
        protected_path(mirror / name)
    if any(os.path.lexists(mirror / name) for name in ("objects/info/alternates", "shallow", "info/grafts")):
        raise IsolationError("observer mirror must be complete and independent")
    allowed = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
               "remote.origin.url", "remote.origin.fetch", "remote.origin.mirror", "extensions.objectformat"}
    settings = _git(clone, "config", "--local", "--no-includes", "--list", "-z").stdout
    for item in settings.split("\0"):
        if item and item.split("\n", 1)[0].lower() not in allowed:
            raise IsolationError("observer mirror contains unsupported Git customization")
    if _git(clone, "rev-parse", "--is-bare-repository").stdout.strip() != "true":
        raise IsolationError("observer requires an independent bare mirror")
    url = _git(clone, "remote", "get-url", "origin").stdout.strip()
    allowed_urls = {f"https://github.com/{repository}.git", f"git@github.com:{repository}.git",
                    f"ssh://git@github.com/{repository}.git"}
    if url not in allowed_urls:
        raise IsolationError("observer mirror remote does not match configured repository")


def fetch(clone):
    _git(clone, "-c", "fetch.recurseSubmodules=false", "fetch", "--no-tags", "--no-recurse-submodules",
         "origin", "+refs/heads/main:refs/remotes/origin/main")

def head_sha(clone):
    return _git(clone, "rev-parse", "origin/main").stdout.strip()

def is_ancestor(clone, old, new):
    r = _git(clone, "merge-base", "--is-ancestor", old, new, check=False)
    if r.returncode not in (0, 1):
        raise RuntimeError("cannot establish checkpoint ancestry")
    return r.returncode == 0

def new_commits(clone, old, new):
    out = _git(clone, "log", "--first-parent", "--reverse", "--format=%H%x1f%an%x1f%s",
               f"{old}..{new}").stdout
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, author, subject = line.split("\x1f", 2)
        commits.append(Commit(sha, author, subject))
    return commits

def _diff_args(clone, sha):
    parents = _git(clone, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    if not parents:
        raise ValueError("commit missing")
    if len(parents) > 1:
        return ["diff", parents[1], parents[0]]
    return ["diff-tree", "--root", "--no-commit-id", "-r", parents[0]]


def commit_diff(clone, sha):
    return _git(clone, *_diff_args(clone, sha), "-p", "--binary",
                "--no-ext-diff", "--no-textconv", "--no-color").stdout


def changed_paths(clone, sha):
    out = _git(clone, *_diff_args(clone, sha), "--name-only", "-z").stdout
    return [path for path in out.split("\0") if path]

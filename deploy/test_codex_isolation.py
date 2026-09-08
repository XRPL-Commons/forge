#!/usr/bin/env python3
"""Synthetic native Codex sandbox probe; run as forge-codex, never as root.

No model login, GitHub operation, real credential, or repository is used. Print a
receipt for the trusted caller to persist. This tests native sandbox execution,
not the managed-profile mount or the exec model/tool adapter.
"""
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import tempfile
import time


PROFILE = '''default_permissions = "forge-fixture"
approval_policy = "never"
[permissions.forge-fixture]
extends = ":workspace"
[permissions.forge-fixture.filesystem]
":root" = "deny"
":minimal" = "read"
"/opt/forge/bin/codex" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"
[permissions.forge-fixture.filesystem.":workspace_roots"]
"." = "write"
[permissions.forge-fixture.network]
enabled = false
[shell_environment_policy]
inherit = "none"
ignore_default_excludes = false
[shell_environment_policy.set]
PATH = "/usr/bin:/bin"
'''


def main():
    account = pwd.getpwnam("forge-codex")
    if os.geteuid() != account.pw_uid or os.geteuid() == 0:
        raise SystemExit("Run this synthetic probe as forge-codex, not root")
    home = Path(account.pw_dir)
    fixture = Path(tempfile.mkdtemp(prefix="native-isolation-", dir=home))
    workspace, config = fixture / "workspace", fixture / "client-config"
    workspace.mkdir()
    config.mkdir()
    (config / "config.toml").write_text(PROFILE)
    secret = fixture / "private-dummy-credential"
    secret.write_text("FORGE_SYNTHETIC_CANARY_NOT_A_REAL_CREDENTIAL")
    secret.chmod(0o600)
    (workspace / "outside-link").symlink_to(secret)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    fd = os.open(secret, os.O_RDONLY)
    parent_pid = os.getpid()
    child = r'''
import json, os, socket
from pathlib import Path
checks = {}
def blocked(name, operation):
    try:
        operation()
    except OSError as error:
        checks[name] = {"passed": True, "error": type(error).__name__}
    else:
        checks[name] = {"passed": False, "error": "operation unexpectedly succeeded"}
Path("workspace-write").write_text("synthetic edit")
checks["workspace_write"] = {"passed": Path("workspace-write").read_text() == "synthetic edit"}
secret = Path(SECRET_PATH)
blocked("private_file_read", lambda: secret.read_text())
blocked("symlink_read", lambda: Path("outside-link").read_text())
blocked("hardlink_creation", lambda: os.link(secret, "hardlink-escape"))
blocked("client_proc_environ", lambda: Path("/proc/" + str(PARENT_PID) + "/environ").read_bytes())
blocked("proc_root_read", lambda: Path("/proc/1/root" + str(secret)).read_text())
blocked("inherited_private_fd", lambda: os.read(PRIVATE_FD, 128))
checks["credential_environment"] = {"passed": not any(k in os.environ for k in
    ("FORGE_TEST_CREDENTIAL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "SSH_AUTH_SOCK"))}
def connect(host, port):
    with socket.socket() as s:
        s.settimeout(.4)
        s.connect((host, port))
blocked("host_loopback_network", lambda: connect("127.0.0.1", HOST_PORT))
blocked("external_network", lambda: connect("192.0.2.1", 443))
print(json.dumps(checks, sort_keys=True))
'''
    for name, value in {"SECRET_PATH": str(secret), "PARENT_PID": parent_pid,
                        "PRIVATE_FD": fd, "HOST_PORT": port}.items():
        child = child.replace(name, repr(value))
    # The trusted launcher removes secrets before passing this debug command.
    # codex sandbox is NOT a test of exec's shell_environment_policy filtering.
    env = {"HOME": str(home), "CODEX_HOME": str(config), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    command = ["/opt/forge/bin/codex", "sandbox", "-P", "forge-fixture", "-C", str(workspace),
               "--", "/usr/bin/python3", "-c", child]
    start = time.monotonic()
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=20, close_fds=True)
        try:
            checks = json.loads(result.stdout)
        except ValueError:
            checks = {}
        accepted = False
        try:
            connection, _ = listener.accept()
            connection.close()
            accepted = True
        except TimeoutError:
            pass
        checks["host_listener_not_reached"] = {"passed": not accepted}
        complete = result.returncode == 0 and len(checks) == 11 and all(c["passed"] for c in checks.values())
        negative_child = "import os,json;print(json.dumps({'private_descriptor_disclosed': os.read(" + str(fd) + ",128) == b'FORGE_SYNTHETIC_CANARY_NOT_A_REAL_CREDENTIAL'}))"
        negative_command = command[:-1] + [negative_child]
        negative = subprocess.run(negative_command, env=env, capture_output=True, text=True,
                                  timeout=20, pass_fds=(fd,))
        receipt = {"negative_control": {"description": "Deliberately forward a private FD; sandbox must not be treated as revoking inherited capabilities", "command": negative_command,
                                       "returncode": negative.returncode, "stdout": negative.stdout, "stderr": negative.stderr},
            "uid": os.geteuid(), "provider_version": subprocess.run(
            ["/opt/forge/bin/codex", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
            "fixture": str(fixture), "command": command, "profile": PROFILE,
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "checks": checks, "passed": complete, "duration": time.monotonic() - start,
            "limitations": ["No real model authentication or repository execution",
                            "No managed requirements mount tested",
                            "Environment check validates clean launcher environment, not exec tool filtering",
                            "External TEST-NET destination failure alone does not establish network denial",
                            "No lifecycle or malicious nested-client persistence proof",
                            "Launcher must close descriptors; negative control deliberately forwards an open dummy credential FD"]}
        print(json.dumps(receipt, indent=2))
        return 0 if complete else 1
    finally:
        os.close(fd)
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())

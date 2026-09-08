"""Unit fault tests; real systemd lifecycle canaries are opt-in via FORGE_SYSTEMD_TESTS=1."""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "safe-build"
loader = importlib.machinery.SourceFileLoader("safe_build", str(WRAPPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
build = importlib.util.module_from_spec(spec)
loader.exec_module(build)


class SafeBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_open_failure_never_launches_build(self):
        with mock.patch.dict(os.environ, {"SAFE_BUILD_LOCK": str(self.directory / "missing" / "lock")}), \
             mock.patch.object(build.subprocess, "Popen") as launch:
            with self.assertRaises(OSError):
                build.inner("test", ["false"], {"wait": .1})
            launch.assert_not_called()

    def test_flock_failure_never_launches_build(self):
        with mock.patch.dict(os.environ, {"SAFE_BUILD_LOCK": str(self.directory / "lock")}), \
             mock.patch.object(build.fcntl, "flock", side_effect=OSError("injected flock failure")), \
             mock.patch.object(build.subprocess, "Popen") as launch:
            with self.assertRaises(OSError):
                build.inner("test", ["false"], {"wait": .1})
            launch.assert_not_called()

    def test_wait_deadline_preserves_other_owner(self):
        first = build.open_lock(self.directory / "lock")
        second = build.open_lock(self.directory / "lock")
        try:
            build.acquire_lock(first, .1)
            with self.assertRaisesRegex(build.BuildError, "wait deadline"):
                build.acquire_lock(second, .03)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(first)
            os.close(second)

    def test_symlink_lock_rejected(self):
        target = self.directory / "target"
        target.touch()
        (self.directory / "lock").symlink_to(target)
        with self.assertRaises(OSError):
            build.open_lock(self.directory / "lock")

    def test_missing_manager_never_launches_build(self):
        with mock.patch.dict(os.environ, {"SAFE_BUILD_LOCK": str(self.directory / "lock")}), \
             mock.patch.object(build, "store_lock", side_effect=build.BuildError("not retained")), \
             mock.patch.object(build.subprocess, "Popen") as launch:
            with self.assertRaises(build.BuildError):
                build.inner("test", ["false"], {"wait": .1})
            launch.assert_not_called()

    def test_descriptor_store_rejection_is_fatal(self):
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": "/ignored"}), \
             mock.patch.object(build.socket, "socket"), \
             mock.patch.object(build.subprocess, "run", return_value=mock.Mock(stdout="0\n")):
            with self.assertRaisesRegex(build.BuildError, "did not retain"):
                build.store_lock(123, "unit")

    def test_nested_invocation_fails_before_service_launch(self):
        with mock.patch.dict(os.environ, {"SAFE_BUILD_ACTIVE": "parent"}), \
             mock.patch.object(build.subprocess, "call") as launch, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(build.main(["true"]), 125)
            launch.assert_not_called()

    def test_resource_caps_cannot_be_increased(self):
        for name, value in (("SAFE_BUILD_MEM_MAX", "7G"), ("SAFE_BUILD_SWAP_MAX", "4G"),
                            ("SAFE_BUILD_HEAP", "4097"), ("SAFE_BUILD_TIMEOUT_SECONDS", "nan")):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}):
                with self.assertRaises(build.BuildError):
                    build.settings()

    def test_metadata_is_private_and_atomic(self):
        directory = self.directory / "state"
        build.write_metadata(directory, {"unit": "one"})
        build.write_metadata(directory, {"unit": "two"})
        self.assertEqual(json.loads((directory / "owner.json").read_text()), {"unit": "two"})
        self.assertEqual((directory / "owner.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(directory.iterdir())), 1)

    def test_service_policy_covers_descendants_and_manager_lock(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            command = build.service_command("forge-build-test.service", ["printf", "%s", "hello"], build.settings())
        for setting in ("KillMode=control-group", "FileDescriptorStoreMax=1", "FileDescriptorStorePreserve=no",
                        "NotifyAccess=main", "Restart=no", "SendSIGKILL=yes"):
            self.assertIn("--property=" + setting, command)
        self.assertEqual(command[-3:], ["printf", "%s", "hello"])
        self.assertTrue(any(arg.startswith("--property=RuntimeMaxSec=") for arg in command))

    def test_command_timeout_returns_124_and_cgroup_teardown_state(self):
        env = {"SAFE_BUILD_LOCK": str(self.directory / "lock"),
               "SAFE_BUILD_STATE_DIR": str(self.directory / "state"), "XDG_RUNTIME_DIR": self.temp.name,
               "NOTIFY_SOCKET": "/private/socket"}
        with mock.patch.dict(os.environ, env), mock.patch.object(build, "store_lock"), \
             mock.patch.object(build.subprocess, "Popen") as launch:
            launch.return_value.wait.side_effect = subprocess.TimeoutExpired("build", .01)
            result = build.inner("test", ["build"], {"wait": .1, "timeout": .01, "heap": "4096"})
        self.assertEqual(result, 124)
        self.assertTrue(launch.call_args.kwargs["close_fds"])
        self.assertNotIn("NOTIFY_SOCKET", launch.call_args.kwargs["env"])
        self.assertNotIn("pass_fds", launch.call_args.kwargs)
        data = json.loads((self.directory / "state" / "owner.json").read_text())
        self.assertEqual(data["state"], "stopping")
        self.assertEqual(data["command_exit"], 124)


@unittest.skipUnless(os.environ.get("FORGE_SYSTEMD_TESTS") == "1", "requires a user systemd manager")
class SystemdLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-build-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = dict(os.environ, SAFE_BUILD_LOCK=str(self.directory / "lock"),
                        SAFE_BUILD_STATE_DIR=str(self.directory / "state"),
                        SAFE_BUILD_TIMEOUT_SECONDS="2", SAFE_BUILD_WAIT_SECONDS="3", SAFE_BUILD_STOP_SECONDS="1")
        self.env.pop("SAFE_BUILD_ACTIVE", None)

    def run_build(self, code, **kwargs):
        return subprocess.run([str(WRAPPER), "/usr/bin/python3", "-c", code], env=self.env,
                              text=True, capture_output=True, timeout=20, **kwargs)

    def assert_lock_free(self):
        fd = build.open_lock(self.directory / "lock")
        try:
            build.acquire_lock(fd, 1)
        finally:
            os.close(fd)

    def test_real_command_result_and_lock_release(self):
        result = self.run_build("raise SystemExit(7)")
        self.assertEqual(result.returncode, 7, result.stderr + result.stdout)
        self.assert_lock_free()

    def test_real_deadline_kills_background_descendant_before_unlock(self):
        pid_file = str(self.directory / "child.pid")
        code = ("import subprocess,time; "
                "child=subprocess.Popen(['/usr/bin/python3','-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
                f"open({pid_file!r},'w').write(str(child.pid)); time.sleep(60)")
        result = self.run_build(code)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(Path(pid_file).exists(), result.stdout + result.stderr)
        pid = int(Path(pid_file).read_text())
        self.assertFalse(Path(f"/proc/{pid}").exists(), result.stdout + result.stderr)
        self.assert_lock_free()

    def test_real_normal_exit_also_kills_background_descendant(self):
        pid_file = str(self.directory / "background.pid")
        code = ("import subprocess; "
                "child=subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(60)']); "
                f"open({pid_file!r},'w').write(str(child.pid))")
        result = self.run_build(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pid = int(Path(pid_file).read_text())
        self.assertFalse(Path(f"/proc/{pid}").exists(), result.stdout + result.stderr)
        self.assert_lock_free()

    def test_real_killed_supervisor_keeps_lock_through_descendant_teardown(self):
        # Repository child kills the service main process. A second independently
        # opened lock must remain unavailable while the TERM-ignoring child lives.
        report = str(self.directory / "report")
        code = ("import os,signal,time,fcntl; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "parent=os.getppid(); os.kill(parent,signal.SIGKILL); time.sleep(.2); "
                f"fd=os.open({self.env['SAFE_BUILD_LOCK']!r},os.O_RDWR); "
                "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); outcome='UNLOCKED'"
                "\nexcept BlockingIOError: outcome='locked'"
                f"\nopen({report!r},'w').write(outcome); time.sleep(60)")
        result = self.run_build(code)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(Path(report).exists(), result.stdout + result.stderr)
        self.assertEqual(Path(report).read_text(), "locked")
        self.assert_lock_free()


if __name__ == "__main__":
    unittest.main()

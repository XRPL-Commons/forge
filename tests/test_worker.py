import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from forge.worker import WorkerTask, WorkerRunner, build_command, parse_output, validate_result


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = WorkerTask("owner/repo:11", "reviewer", "a" * 40, "Review supplied patch")
        self.output = {"task_id": self.task.task_id, "role": self.task.role,
                       "candidate": self.task.candidate, "status": "completed",
                       "summary": "Reviewed supplied patch", "findings": []}

    def fake_cli(self, source):
        path = self.root / "fake-cli"
        path.write_text("#!/usr/bin/python3\n" + source)
        path.chmod(0o700)
        return str(path)

    def test_both_structured_transports_and_exact_candidate(self):
        streams = {
            "codex": [{"type": "thread.started", "thread_id": "session-1"}, {"type": "turn.completed"}],
            "claude": [{"type": "result", "subtype": "success", "is_error": False, "session_id": "session-1",
                        "structured_output": self.output}],
        }
        for provider, events in streams.items():
            result = parse_output(provider, "\n".join(map(json.dumps, events)), "", 0,
                                  self.task, json.dumps(self.output))
            self.assertEqual(result[0], "completed")
            self.assertEqual(result[1], "session-1")
        wrong = dict(self.output, candidate="other")
        with self.assertRaises(ValueError):
            validate_result(wrong, self.task)

    def test_invalid_output_never_counts_as_success(self):
        for output in ("", "not JSON", "{}", "null"):
            self.assertEqual(parse_output("codex", output, "", 0, self.task)[0], "failed_retryable")
        with self.assertRaises(ValueError):
            validate_result(dict(self.output, findings=[{"path": "../secret", "line": True,
                                                       "message": "bad", "severity": "high"}]), self.task)

    def test_health_uses_error_channels_not_model_prose(self):
        output = dict(self.output, summary="Example text says out of credits and token expired")
        stream = json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s",
                             "structured_output": output})
        self.assertEqual(parse_output("claude", stream, "", 0, self.task)[0], "completed")
        for error, expected in [("credit balance exhausted", "blocked_quota"),
                                ("authentication_error", "blocked_auth"),
                                ("permission denied", "blocked_permission")]:
            self.assertEqual(parse_output("codex", "", error, 1, self.task)[0], expected)

    def test_explicit_resume_and_no_bypass_commands(self):
        task = WorkerTask("task", "auditor", "sha", "test", "session-123")
        for provider in ("codex", "claude", "grok"):
            command = build_command(provider, task, self.root)
            self.assertIn("session-123", command)
            self.assertFalse(any("dangerously" in c or "always-approve" in c or "bypassPermissions" in c for c in command))
            self.assertNotIn("--continue", command)
        self.assertIn("--tools", build_command("claude", task, self.root))

    def test_grok_transport_is_model_only_and_reads_private_prompt_file(self):
        command = build_command("grok", self.task, self.root)
        self.assertEqual(command[:3], ["grok", "--prompt-file", str(self.root / "input.txt")])
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--output-format") + 1], "streaming-messages-json")
        for flag in ("--no-subagents", "--disable-web-search", "--no-auto-update", "--json-schema"):
            self.assertIn(flag, command)
        self.assertNotIn("--resume", command)
        # Verbatim unauthenticated output of Grok Build 1.0.34: an account outage, never a task failure.
        stdout = '{"type":"system","subtype":"init","session_id":"","apiKeySource":"user","model":"unknown","cwd":"","permissionMode":"default","tools":[],"slash_commands":[],"mcp_servers":[],"skills":[],"uuid":"dc2d3aaf-867b-4cc6-b2d5-a4fb3f9577e6"}\\n{"type":"result","subtype":"error_during_execution","is_error":true,"duration_ms":0,"duration_api_ms":0,"num_turns":0,"stop_reason":null,"total_cost_usd":0.0,"usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"server_tool_use":{"web_search_requests":0}},"modelUsage":{},"errors":["Not signed in. To authenticate without a browser, run:\\\\n  grok login --device-code\\\\n\\\\nAlternatively, set the XAI_API_KEY environment variable or run `grok login` on a machine with a browser."],"session_id":"","uuid":"bd3b392b-5243-437a-a015-414cdc1520eb"}\\n'
        status, session, output, _ = parse_output("grok", stdout, "Error: Not signed in.\n", 1, self.task)
        self.assertEqual((status, output), ("blocked_auth", None))
        success = [{"type": "system", "subtype": "init", "session_id": "grok-1"},
                   {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}, "session_id": "grok-1"},
                   {"type": "result", "subtype": "success", "is_error": False, "session_id": "grok-1", "structured_output": self.output}]
        status, session, output, _ = parse_output("grok", "\n".join(map(json.dumps, success)), "", 0, self.task)
        self.assertEqual((status, session, output), ("completed", "grok-1", self.output))
        tooled = success[:1] + [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read_file", "input": {}}]}, "session_id": "grok-1"}] + success[2:]
        self.assertEqual(parse_output("grok", "\n".join(map(json.dumps, tooled)), "", 0, self.task)[0], "blocked_permission")

    def test_fake_grok_run_uses_prompt_file_and_scoped_environment(self):
        event = {"type": "result", "subtype": "success", "is_error": False, "session_id": "g", "structured_output": self.output}
        script = self.fake_cli(
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "assert args[args.index('--tools') + 1] == ''\n"
            "assert 'GH_TOKEN' not in os.environ and 'ANTHROPIC_API_KEY' not in os.environ\n"
            "assert os.environ.get('GROK_HOME') == 'fixture-home'\n"
            "prompt = open(args[args.index('--prompt-file') + 1]).read()\n"
            "assert 'model-only Forge analysis worker' in prompt and 'owner/repo:11' in prompt\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'g'}))\n"
            "print(" + repr(json.dumps(event)) + ")\n")
        previous = {key: os.environ.get(key) for key in ("GH_TOKEN", "ANTHROPIC_API_KEY", "GROK_HOME")}
        os.environ.update(GH_TOKEN="fixture-secret", ANTHROPIC_API_KEY="fixture-secret", GROK_HOME="fixture-home")
        try:
            result = WorkerRunner().run(self.task, provider="grok", attempt_dir=self.root / "grok-run", executable=script)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual((result.status, result.session_id, result.provider), ("completed", "g", "grok"))
        self.assertTrue(result.process_group_stopped)
        self.assertEqual(json.loads((self.root / "grok-run/command.json").read_text())[1], "--prompt-file")

    def test_fake_claude_success_records_receipt_without_inheriting_gh_token(self):
        event = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "structured_output": self.output}
        script = self.fake_cli("import json, os\nassert 'GH_TOKEN' not in os.environ\nprint(" + repr(json.dumps(event)) + ")\n")
        old = os.environ.get("GH_TOKEN")
        os.environ["GH_TOKEN"] = "fixture-secret"
        try:
            result = WorkerRunner().run(self.task, provider="claude", attempt_dir=self.root / "run", executable=script)
        finally:
            if old is None:
                os.environ.pop("GH_TOKEN")
            else:
                os.environ["GH_TOKEN"] = old
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.process_group_stopped)
        self.assertTrue((self.root / "run/outcome.json").exists())

    def test_timeout_cancel_startup_and_live_gate(self):
        script = self.fake_cli("import time\ntime.sleep(30)\n")
        runner = WorkerRunner()
        result = runner.run(self.task, provider="claude", attempt_dir=self.root / "timeout", executable=script, timeout=0.1)
        self.assertEqual(result.status, "failed_retryable")
        self.assertTrue(result.process_group_stopped)
        self.assertLess(result.duration, 2)
        result = runner.run(self.task, provider="claude", attempt_dir=self.root / "cancel", executable=script, cancel=lambda: True)
        self.assertEqual(result.status, "cancelled")
        result = runner.run(self.task, provider="claude", attempt_dir=self.root / "missing", executable="/missing/forge-cli")
        self.assertEqual(result.status, "failed_retryable")
        for provider, mode in (("codex", "model_only"), ("claude", "live")):
            result = runner.run(self.task, provider=provider, attempt_dir=self.root / "blocked", mode=mode)
            self.assertEqual(result.status, "blocked_permission")
        self.assertFalse((self.root / "blocked").exists())

    def test_fresh_attempt_directory_and_output_limit(self):
        script = self.fake_cli("print('x' * 20000)\n")
        result = WorkerRunner().run(self.task, provider="claude", attempt_dir=self.root / "limited",
                                    executable=script, max_output_bytes=1024)
        self.assertEqual(result.status, "failed_retryable")
        with self.assertRaises(FileExistsError):
            WorkerRunner().run(self.task, provider="claude", attempt_dir=self.root / "limited", executable=script)

    def test_codex_fake_transport_and_descendant_deadline_cleanup(self):
        source = ("import sys,json\nfrom pathlib import Path\n"
                  "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("
                  + repr(json.dumps(self.output)) + ")\n"
                  "print(json.dumps({'type':'thread.started','thread_id':'thread-1'}))\n"
                  "print(json.dumps({'type':'turn.completed'}))\n")
        script = self.fake_cli(source)
        result = WorkerRunner().run(self.task, provider="codex", attempt_dir=self.root / "codex",
                                    executable=script, mode="transport_smoke")
        self.assertEqual(result.status, "completed")
        marker = self.root / "child-survived"
        child = "import time;from pathlib import Path;time.sleep(.5);Path(" + repr(str(marker)) + ").touch()"
        script = self.fake_cli("import subprocess,time\nsubprocess.Popen(['/usr/bin/python3','-c',"
                               + repr(child) + "])\ntime.sleep(30)\n")
        result = WorkerRunner().run(self.task, provider="claude", attempt_dir=self.root / "descendant",
                                    executable=script, timeout=.15)
        self.assertTrue(result.process_group_stopped)
        time.sleep(.6)
        self.assertFalse(marker.exists(), "child continued executing after parent deadline")

    def test_ambiguous_completion_and_resume_identity_are_rejected(self):
        output = json.dumps(self.output)
        events = [{'type':'thread.started', 'thread_id':'session-111'}, {'type':'turn.completed'}]
        duplicate = '\n'.join(json.dumps(e) for e in events + [{'type':'turn.completed'}])
        self.assertEqual(parse_output('codex', duplicate, '', 0, self.task, output)[0], 'failed_retryable')
        resumed = WorkerTask(self.task.task_id, self.task.role, self.task.candidate, self.task.prompt, 'session-222')
        self.assertEqual(parse_output('codex', '\n'.join(map(json.dumps, events)), '', 0, resumed, output)[0], 'failed_retryable')
        changed = events[:1] + [{'type':'thread.started', 'thread_id':'session-222'}] + events[1:]
        self.assertEqual(parse_output('codex', '\n'.join(map(json.dumps, changed)), '', 0, self.task, output)[0], 'failed_retryable')
        missing_flag = json.dumps({'type':'result','subtype':'success','session_id':'session-111','structured_output':self.output})
        self.assertEqual(parse_output('claude', missing_flag, '', 0, self.task)[0], 'failed_retryable')

    def test_unexpected_tools_are_policy_failures_not_success(self):
        codex = [{'type':'thread.started','thread_id':'session-111'},
                 {'type':'item.completed','item':{'type':'command_execution','command':'fixture'}},
                 {'type':'turn.completed'}]
        self.assertEqual(parse_output('codex', '\n'.join(map(json.dumps,codex)), '', 0, self.task, json.dumps(self.output))[0], 'blocked_permission')
        for tool in ('Bash','Read','Edit','WebFetch'):
            events = [{'type':'assistant','message':{'content':[{'type':'tool_use','name':tool}]}},
                      {'type':'result','subtype':'success','is_error':False,'session_id':'session-111','structured_output':self.output}]
            self.assertEqual(parse_output('claude', '\n'.join(map(json.dumps,events)), '', 0, self.task)[0], 'blocked_permission')

    def test_private_descriptor_and_unrelated_environment_do_not_reach_cli(self):
        private = self.root / 'dummy-private'
        private.write_text('synthetic-only')
        fd = os.open(private, os.O_RDONLY)
        os.set_inheritable(fd, True)
        event = {'type':'result','subtype':'success','is_error':False,'session_id':'session-111','structured_output':self.output}
        source = ('import os\ntry:\n os.read(' + str(fd) + ', 128)\nexcept OSError:\n pass\nelse:\n raise AssertionError("private descriptor inherited")\n'
                  'assert "SSH_AUTH_SOCK" not in os.environ\nassert "ANTHROPIC_BASE_URL" not in os.environ\nprint(' + repr(json.dumps(event)) + ')\n')
        script = self.fake_cli(source)
        from unittest.mock import patch
        try:
            with patch.dict(os.environ, {'SSH_AUTH_SOCK':'/synthetic/socket', 'ANTHROPIC_BASE_URL':'http://untrusted.invalid'}):
                result = WorkerRunner().run(self.task, provider='claude', attempt_dir=self.root/'fd-check', executable=script)
        finally:
            os.close(fd)
        self.assertEqual(result.status, 'completed')

    def test_nonregular_result_never_blocks_read_or_discloses_file(self):
        source = ('import os,json,sys\nfrom pathlib import Path\n'
                  'path=Path(sys.argv[sys.argv.index("--output-last-message")+1])\nos.mkfifo(path)\n'
                  'print(json.dumps({"type":"thread.started","thread_id":"session-111"}))\n'
                  'print(json.dumps({"type":"turn.completed"}))\n')
        result = WorkerRunner().run(self.task, provider='codex', attempt_dir=self.root/'fifo',
                                    executable=self.fake_cli(source), mode='transport_smoke', timeout=1)
        self.assertEqual(result.status, 'failed_retryable')
        self.assertLess(result.duration, 2)
        self.assertIn('invalid result artifact', result.reason)

    def test_codex_auth_failure_without_final_file_keeps_account_classification(self):
        script = self.fake_cli('import sys\nprint("authentication_error",file=sys.stderr)\nsys.exit(1)\n')
        result = WorkerRunner().run(self.task, provider='codex', attempt_dir=self.root/'auth', executable=script, mode='transport_smoke')
        self.assertEqual(result.status, 'blocked_auth')

    def test_nonfinite_deadlines_are_rejected_before_launch(self):
        for timeout in (float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                WorkerRunner().run(self.task, provider='claude', attempt_dir=self.root/'invalid', timeout=timeout)
        self.assertFalse((self.root/'invalid').exists())

    def test_termination_signal_reaps_cli_and_persists_cancelled_receipt(self):
        import signal
        import subprocess
        import sys
        marker = self.root/'cli-survived'
        script = self.fake_cli('import time\nfrom pathlib import Path\ntime.sleep(1)\nPath(' + repr(str(marker)) + ').touch()\ntime.sleep(30)\n')
        attempt = self.root/'signal-run'
        source = ('from forge.worker import WorkerTask,WorkerRunner\n'
                  'WorkerRunner().run(WorkerTask("task","reviewer","candidate","fixture"), provider="claude",'
                  'attempt_dir=' + repr(str(attempt)) + ',executable=' + repr(script) + ')\n')
        process = subprocess.Popen([sys.executable,'-c',source], start_new_session=True)
        try:
            deadline = time.monotonic()+3
            while not (attempt/'process.json').exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue((attempt/'process.json').exists())
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=3)
            outcome=json.loads((attempt/'outcome.json').read_text())
            self.assertEqual(outcome['status'],'cancelled')
            self.assertTrue(outcome['process_group_stopped'])
            time.sleep(1.05)
            self.assertFalse(marker.exists())
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait()

    def test_host_sandbox_failure_is_not_a_provider_outage(self):
        status = parse_output('codex', '', 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted', 1, self.task)[0]
        self.assertEqual(status, 'failed_infrastructure')
        script = self.fake_cli('import time\ntime.sleep(30)\n')
        def broken_callback():
            raise RuntimeError('controller fixture failure')
        result = WorkerRunner().run(self.task, provider='claude', attempt_dir=self.root/'callback',
                                    executable=script, cancel=broken_callback)
        self.assertEqual(result.status, 'failed_infrastructure')
        self.assertTrue(result.process_group_stopped)
        self.assertTrue((self.root/'callback/outcome.json').exists())
        Path(script).chmod(0o600)
        result = WorkerRunner().run(self.task, provider='claude', attempt_dir=self.root/'permission', executable=script)
        self.assertEqual(result.status, 'blocked_permission')

    def test_resume_startup_auth_failure_is_not_hidden_by_missing_session_event(self):
        task = WorkerTask(self.task.task_id, self.task.role, self.task.candidate, self.task.prompt, 'session-111')
        self.assertEqual(parse_output('codex', '', 'authentication_error', 1, task)[0], 'blocked_auth')
        self.assertEqual(parse_output('claude', '', 'out of credits', 1, task)[0], 'blocked_quota')

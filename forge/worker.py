"""Bounded headless transports. These do NOT authorize repository execution.

Claude model_only removes all tools. Codex transport_smoke is deliberately opt-in
and only for trusted synthetic inputs: read-only is not credential isolation.
Grok Build model_only uses the same Messages-API wire stream as Claude with an
empty tool allowlist; its prompt is read from the attempt's private input file
because headless Grok never reads stdin. Any observed tool use is a policy
failure. Live coding requires a separate, externally enforced executor not
implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import threading
import subprocess
import time
from typing import Callable

from .state import atomic_json, PROVIDERS, ROLES


RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "task_id": {"type": "string"}, "role": {"type": "string", "enum": sorted(ROLES)},
        "candidate": {"type": "string"},
        "status": {"type": "string", "enum": ["completed", "failed_task", "blocked_permission"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"}, "line": {"type": "integer", "minimum": 1},
                           "message": {"type": "string"},
                           "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]}},
            "required": ["path", "line", "message", "severity"]}},
    },
    "required": ["task_id", "role", "candidate", "status", "summary", "findings"],
}


@dataclass(frozen=True)
class WorkerTask:
    task_id: str
    role: str
    candidate: str
    prompt: str
    session_id: str | None = None

    def __post_init__(self):
        if any(not isinstance(v, str) or not v for v in (self.task_id, self.role, self.candidate, self.prompt)) or self.role not in ROLES:
            raise ValueError("task identity, role, candidate and prompt required")
        if self.session_id is not None and (not isinstance(self.session_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", self.session_id)):
            raise ValueError("explicit session identifier required")


@dataclass(frozen=True)
class WorkerResult:
    status: str
    provider: str
    session_id: str | None
    output: dict | None
    exit_code: int | None
    reason: str
    process_group_stopped: bool
    duration: float


def validate_result(value: object, task: WorkerTask) -> dict:
    if not isinstance(value, dict) or set(value) != set(RESULT_SCHEMA["required"]):
        raise ValueError("invalid result fields")
    if any(not isinstance(value[k], str) for k in ("task_id", "role", "candidate", "status", "summary")):
        raise ValueError("invalid result field types")
    if (value["task_id"], value["role"], value["candidate"]) != (task.task_id, task.role, task.candidate):
        raise ValueError("result does not match exact task and candidate")
    if value["status"] not in {"completed", "failed_task", "blocked_permission"}:
        raise ValueError("invalid result status")
    if not isinstance(value["findings"], list):
        raise ValueError("findings must be a list")
    for finding in value["findings"]:
        if not isinstance(finding, dict) or set(finding) != {"path", "line", "message", "severity"}:
            raise ValueError("invalid finding")
        if (not isinstance(finding["path"], str) or not finding["path"]
                or any(ord(c) < 32 for c in finding["path"]) or finding["path"] in {".", "./"}
                or Path(finding["path"]).is_absolute() or ".." in Path(finding["path"]).parts
                or type(finding["line"]) is not int or finding["line"] < 1
                or not isinstance(finding["message"], str) or not finding["message"]
                or finding["severity"] not in {"low", "medium", "high", "critical"}):
            raise ValueError("finding requires relative file and positive line")
    return value


def build_command(provider: str, task: WorkerTask, attempt_dir: Path,
                  executable: str | None = None) -> list[str]:
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    binary = executable or provider
    if provider == "grok":
        # Verified against Grok Build 1.0.34: flags parse; tool suppression is
        # additionally enforced by rejecting any tool_use block in the stream.
        command = [binary, "--prompt-file", str(attempt_dir / "input.txt"), "--tools", "",
                   "--permission-mode", "dontAsk", "--no-subagents", "--disable-web-search",
                   "--no-auto-update", "--output-format", "streaming-messages-json",
                   "--json-schema", json.dumps(RESULT_SCHEMA, separators=(",", ":"))]
        return command + (["--resume", task.session_id] if task.session_id else [])
    if provider == "codex":
        command = [binary, "exec"] + (["resume"] if task.session_id else [])
        command += ["--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                    "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
                    "--disable", "shell_tool", "--disable", "unified_exec",
                    "--json", "--output-schema", str(attempt_dir / "schema.json"),
                    "--output-last-message", str(attempt_dir / "result.json")]
        return command + ([task.session_id] if task.session_id else []) + ["-"]
    command = [binary, "-p", "--restricted", "--safe-mode", "--strict-mcp-config",
               "--tools", "", "--permission-prompts", "none", "--disable-slash-commands",
               "--output-format", "stream-json", "--verbose", "--json-schema",
               json.dumps(RESULT_SCHEMA, separators=(",", ":"))]
    return command + (["--resume", task.session_id] if task.session_id else [])


def classify_error(text: str) -> str:
    """Use only CLI error channels, never successful model prose, for health."""
    lowered = text.lower()
    if any(s in lowered for s in ("bwrap:", "bubblewrap is required", "no space left on device",
                                  "cannot allocate memory", "sandbox could not initialize")):
        return "failed_infrastructure"
    if any(s in lowered for s in ("permission denied", "approval required", "sandbox denied")):
        return "blocked_permission"
    if any(s in lowered for s in ("insufficient_quota", "credit balance", "usage limit",
                                  "out of credits", "exhausted credits")):
        return "blocked_quota"
    if any(s in lowered for s in ("authentication_error", "invalid_api_key", "not logged in", "not signed in",
                                  "please login", "please log in", "token expired", "unauthorized")):
        return "blocked_auth"
    return "failed_retryable"


def parse_output(provider: str, stdout: str, stderr: str, exit_code: int,
                 task: WorkerTask, result_text: str | None = None) -> tuple[str, str | None, dict | None, str]:
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    session = None
    final = None
    completed = False
    errors = []
    malformed = False
    unsafe_tools = False
    completions = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        kind = event.get("type")
        if provider == "codex":
            if kind == "thread.started":
                new_session = event.get("thread_id")
                if session is not None and session != new_session:
                    malformed = True
                session = new_session
            elif kind == "turn.completed":
                completions += 1
                completed = True
            elif kind in {"item.started", "item.completed"}:
                item = event.get("item", {})
                if not isinstance(item, dict) or item.get("type") not in {"agent_message", "reasoning"}:
                    unsafe_tools = True
            elif kind in {"error", "turn.failed"}:
                errors.append(json.dumps(event))
        else:
            # Claude stream-json and Grok Build streaming-messages-json share this shape.
            if isinstance(event.get("session_id"), str):
                if session is not None and session != event["session_id"]:
                    malformed = True
                session = event["session_id"]
            message = event.get("message", {})
            if isinstance(message, dict):
                for block in message.get("content", []) if isinstance(message.get("content", []), list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
                        unsafe_tools = True
            if kind == "result":
                completions += 1
                completed = event.get("subtype") == "success" and event.get("is_error") is False
                if completed:
                    final = event.get("structured_output")
                else:
                    errors.append(json.dumps(event))
    if unsafe_tools:
        return "blocked_permission", session, None, "unexpected tool execution in model-only transport"
    if exit_code or errors:
        return classify_error(stderr + "\n" + "\n".join(errors)), session, None, "CLI reported failure"
    if task.session_id is not None and session != task.session_id:
        return "failed_retryable", session, None, "resumed session identity mismatch"
    if malformed or completions != 1 or not completed or not isinstance(session, str) or not session:
        return "failed_retryable", session, None, "incomplete or malformed event stream"
    if provider == "codex":
        try:
            final = json.loads(result_text or "")
        except ValueError:
            return "failed_retryable", session, None, "missing or malformed final result"
    try:
        output = validate_result(final, task)
    except (ValueError, TypeError) as error:
        return "failed_retryable", session, None, str(error)
    return output["status"], session, output, "validated model result; independent verification required"


def _group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def stop_process_group(process: subprocess.Popen, grace: float = 0.5) -> bool:
    """Kill inherited group even if leader exited. setsid escapes require a cgroup."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        if not _group_exists(process.pid):
            return True
        time.sleep(min(grace, 0.05))
    return not _group_exists(process.pid)



@contextmanager
def _termination_requested():
    """Main-thread CLI runner catches termination long enough to reap its group.

    Embedded threaded callers must use the cancellation callback or own service
    cgroup; Python cannot install signal handlers outside its main thread.
    """
    requested = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda signum, frame: requested.set())
    try:
        yield requested
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _read_artifact(path: Path, limit: int) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("result artifact must be a regular file without hardlinks")
        if metadata.st_size > limit:
            raise ValueError("result artifact exceeds output limit")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("result artifact exceeds output limit")
        return content.decode("utf-8", errors="replace")


class WorkerRunner:
    def run(self, task: WorkerTask, *, provider: str, attempt_dir: str | Path,
            timeout: float = 300, executable: str | None = None,
            mode: str = "model_only", cancel: Callable[[], bool] | None = None,
            max_output_bytes: int = 8 * 1024 * 1024) -> WorkerResult:
        if (provider not in PROVIDERS or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0 or type(max_output_bytes) is not int or max_output_bytes < 1024):
            raise ValueError("invalid provider or execution limits")
        if mode not in {"model_only", "transport_smoke"} or (provider == "codex" and mode != "transport_smoke"):
            return WorkerResult("blocked_permission", provider, None, None, None,
                                "live execution disabled; Codex requires explicit trusted transport_smoke",
                                True, 0)
        directory = Path(attempt_dir).resolve()
        # A fresh directory prevents stale output from being accepted on a failed run.
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        context = directory / "context"
        context.mkdir(mode=0o700)
        atomic_json(directory / "schema.json", RESULT_SCHEMA)
        atomic_json(directory / "task.json", asdict(task))
        command = build_command(provider, task, directory, executable)
        atomic_json(directory / "command.json", command)
        prompt = ("You are a model-only Forge analysis worker. Do not execute commands. "
                  "Return the required JSON for this exact task. Findings must cite relative files and lines.\n"
                  + json.dumps(asdict(task), ensure_ascii=False))
        prompt_path = directory / "input.txt"
        prompt_path.write_text(prompt)
        os.chmod(prompt_path, 0o600)
        # Explicit client auth context only. Repository execution is not admitted.
        names = {"PATH", "HOME", "LANG", "LC_ALL", "TERM"}
        names |= {"codex": {"CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY"},
                  "claude": {"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"},
                  "grok": {"GROK_HOME", "XAI_API_KEY"}}[provider]
        env = {key: value for key, value in os.environ.items() if key in names}
        env.update(NO_COLOR="1", TERM="dumb")
        start = time.monotonic()
        stdout_path, stderr_path = directory / "events.jsonl", directory / "stderr.log"
        process = None
        outcome = None
        stopped = True
        with _termination_requested() as termination, prompt_path.open("rb") as stdin, stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            try:
                process = subprocess.Popen(command, cwd=context, env=env, stdin=stdin,
                                           stdout=stdout, stderr=stderr, start_new_session=True,
                                           close_fds=True)
                atomic_json(directory / "process.json", {"pid": process.pid, "process_group": process.pid,
                                                         "started_at": time.time()})
                while process.poll() is None:
                    if termination.is_set() or (cancel is not None and cancel()):
                        outcome = ("cancelled", "controller cancelled attempt")
                        break
                    if time.monotonic() - start >= timeout:
                        outcome = ("failed_retryable", "attempt deadline exceeded")
                        break
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
                        outcome = ("failed_retryable", "output limit exceeded")
                        break
                    time.sleep(min(0.05, timeout / 10))
                if termination.is_set():
                    outcome = ("cancelled", "runner received termination signal")
            except PermissionError:
                outcome = ("blocked_permission", "CLI launch or artifact access denied")
            except OSError as error:
                outcome = (classify_error(str(error)), "CLI startup failed: " + error.__class__.__name__)
            except Exception as error:
                outcome = ("failed_infrastructure", "trusted runner callback failed: " + error.__class__.__name__)
            finally:
                if process is not None:
                    stopped = stop_process_group(process)
        if not stopped:
            outcome = ("failed_infrastructure", "process group remains; replacement prohibited")
        code = process.returncode if process is not None else None
        if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
            outcome = outcome or ("failed_retryable", "output limit exceeded")
        if outcome:
            result = WorkerResult(outcome[0], provider, None, None, code, outcome[1],
                                  stopped, time.monotonic() - start)
        else:
            try:
                final = _read_artifact(directory / "result.json", max_output_bytes) if provider == "codex" and code == 0 else None
                status, session, output, reason = parse_output(
                    provider, _read_artifact(stdout_path, max_output_bytes), _read_artifact(stderr_path, max_output_bytes),
                    code, task, final)
            except (OSError, ValueError) as error:
                status, session, output, reason = "failed_retryable", None, None, "invalid result artifact: " + type(error).__name__
            result = WorkerResult(status, provider, session, output, code, reason, stopped,
                                  time.monotonic() - start)
        atomic_json(directory / "outcome.json", asdict(result))
        return result

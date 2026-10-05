"""What a running discussion worker is doing, as short operator-facing lines.

Input is the worker's bounded, already scrubbed `docker logs` tail:
- `forge.activity` events the Claude and Grok adapters print for each tool
  call (forge/harnesses/activity.py: tool name and a few truncated inputs);
- Codex's own `exec --json` events (`item.started`, `item.completed`);
- Claude stream-json `assistant` events, should a harness print them raw;
- the entrypoint's `{"runner": "alive"}` heartbeat, for coarse phases.

Lines are gerund phrases ("Reading issue #7"), so the UI can say them as the
persona ("Ember is reading issue #7"). Never raw tool output, file contents or
whole commands: at most a program and one argument, every line scrubbed and
capped. Lines live in the discussion record only, never the factory ledger.
"""
import json
import re
import shlex
from pathlib import PurePosixPath

from .runner import scrub

LIMIT = 100
KEEP = 8
REPOSITORY = "/work/repository/"
STARTED = "Getting started"
EVIDENCE = "Reading the saved evidence"
INSPECTING = "Inspecting the repository"
WRITING = "Writing the answer"
THINKING = "Thinking it through"
# Something that looks like a credential never reaches a line, even truncated.
SECRETISH = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|bearer|cookie|credential)")
WORD = re.compile(r"[^\w./:#@%+,=*?-]")


def cap(text, limit=LIMIT):
    text = scrub(" ".join(str(text).split()))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def short(value, limit=40):
    """One argument, safe to show: no credential-looking text, no shell noise."""
    value = WORD.sub("", str(value)).strip()
    if not value or SECRETISH.search(value) or re.search(r"[A-Za-z0-9_-]{32,}", value) or scrub(value) != value:
        return None
    return value if len(value) <= limit else value[:limit - 1] + "…"


def where(path):
    path = str(path)
    if path.startswith(REPOSITORY):
        path = path[len(REPOSITORY):]
    elif path.startswith("/"):
        # The worker's own notes, image files and the like: not the repository.
        return None
    return short(path, 60)


def reading(path):
    target = where(path)
    return f"Reading {target}" if target else EVIDENCE


def searching(pattern):
    shown = short(pattern)
    return f"Searching the repository for `{shown}`" if shown else "Searching the repository"


def arguments(tokens):
    return [t for t in tokens[1:] if not t.startswith("-")]


def number(tokens):
    return next((t.lstrip("#") for t in tokens if re.fullmatch(r"#?[0-9]{1,7}", t)), None)


def command_line(command):
    """`bash -lc "cd /work/repository && rg coach src"` → "Searching the repository for `coach`"."""
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    command = str(command)
    wrapped = re.match(r"^\s*(?:/usr)?(?:/bin/)?(?:ba|z)?sh\s+-l?c\s+(.*)$", command, re.S)
    if wrapped:
        try:
            command = shlex.split(wrapped.group(1))[0]
        except (ValueError, IndexError):
            command = wrapped.group(1)
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]  # Environment assignments may carry credentials.
        if not tokens or PurePosixPath(tokens[0]).name in ("cd", "export", "set", "true", "echo", "printf", "pwd"):
            continue
        return program_line(PurePosixPath(tokens[0]).name, tokens)
    return INSPECTING


def program_line(program, tokens):
    args = arguments(tokens)
    sub = args[:2]
    if program == "gh":
        if sub[:1] == ["issue"]:
            found = number(args)
            return f"Reading issue #{found}" if found else "Reading the issue on GitHub"
        if sub[:1] == ["pr"]:
            found = number(args)
            if sub[1:] == ["list"]:
                return "Listing pull requests"
            return f"Opening PR #{found}" if found else "Opening the pull request"
        if sub[:1] in (["run"], ["workflow"]):
            return "Checking CI runs"
        return "Querying GitHub"
    if program == "git":
        if sub[:1] == ["grep"] and len(args) > 1:
            return searching(args[1])
        if sub[:1] in (["log"], ["show"], ["diff"], ["blame"], ["shortlog"]):
            return "Inspecting git history"
        return "Checking the git checkout"
    if program in ("rg", "grep", "ag", "ack", "egrep", "fgrep"):
        return searching(args[0]) if args else "Searching the repository"
    if program in ("cat", "head", "tail", "sed", "less", "nl", "bat", "more", "wc", "jq", "awk"):
        files = [a for a in args if "/" in a or "." in a]
        return reading(files[-1]) if files else EVIDENCE
    if program in ("ls", "tree", "find", "fd", "du", "stat", "file"):
        if program == "find" and "-name" in tokens:
            index = tokens.index("-name")
            if index + 1 < len(tokens):
                shown = short(tokens[index + 1])
                if shown:
                    return f"Looking for files matching `{shown}`"
        return "Looking around the repository"
    if program in ("npm", "pnpm", "yarn", "bun"):
        if sub[:1] == ["run"] and len(args) > 1 and short(args[1], 30):
            return f"Running {program} run {short(args[1], 30)}"
        if sub and short(sub[0], 30):
            return f"Running {program} {short(sub[0], 30)}"
        return f"Running {program}"
    if program in ("npx", "pnpx", "bunx", "python", "python3", "pytest", "node", "go", "cargo", "make", "uv", "pip", "tsc", "vitest"):
        shown = short(args[0], 30) if args and not args[0].startswith("/") else None
        return f"Running {program} {shown}" if shown else f"Running {program}"
    shown = short(program, 30)
    return f"Running {shown}" if shown else INSPECTING


def tool_line(name, data):
    data = data if isinstance(data, dict) else {}
    if name in ("Read", "NotebookRead"):
        return reading(data.get("file_path") or data.get("path") or "")
    if name == "Grep":
        return searching(data.get("pattern", ""))
    if name == "Glob":
        shown = short(data.get("pattern", ""))
        return f"Looking for files matching `{shown}`" if shown else "Looking around the repository"
    if name == "LS":
        return "Looking around the repository"
    if name == "Bash":
        return command_line(data.get("command", ""))
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        target = where(data.get("file_path") or "")
        return f"Editing {target}" if target else "Taking notes"
    if name == "StructuredOutput":
        return WRITING
    if name == "TodoWrite":
        return "Planning the next steps"
    if name in ("WebFetch", "WebSearch"):
        return "Searching the web"
    if name == "Task":
        return "Handing a sub-task to a helper"
    if name == "text":
        return THINKING
    shown = short(name, 30)
    return f"Using {shown}" if shown else THINKING


def events(logs):
    """(identity, at or None, line) for each recognised event, oldest first."""
    found = []
    for raw in logs.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if event.get("runner") == "alive":
            found.append(("alive", event.get("at"), None))
        elif kind == "forge.activity":
            found.append(("tool:" + str(event.get("id", ""))[:64], event.get("at"), tool_line(str(event.get("tool", "")), event.get("input"))))
        elif kind == "assistant":
            content = (event.get("message") or {}).get("content") or []
            for index, part in enumerate(content if isinstance(content, list) else []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    found.append(("tool:" + str(part.get("id", ""))[:64], None, tool_line(str(part.get("name", "")), part.get("input"))))
                elif part.get("type") == "text" and str(part.get("text", "")).strip():
                    found.append(("text:" + str((event.get("message") or {}).get("id", ""))[:64] + ":" + str(index), None, THINKING))
        elif kind in ("item.started", "item.completed") and isinstance(event.get("item"), dict):
            item = event["item"]
            identity = "codex:" + str(item.get("id", ""))[:64]
            if item.get("type") == "command_execution" and kind == "item.started":
                found.append((identity, None, command_line(item.get("command", ""))))
            elif item.get("type") == "reasoning" and kind == "item.completed":
                found.append((identity, None, THINKING))
            elif item.get("type") == "agent_message" and kind == "item.completed":
                found.append((identity, None, WRITING))
            elif item.get("type") == "file_change" and kind == "item.started":
                found.append((identity, None, "Taking notes"))
            elif item.get("type") == "web_search" and kind == "item.started":
                found.append((identity, None, "Searching the web"))
            elif item.get("type") == "mcp_tool_call" and kind == "item.started":
                found.append((identity, None, "Using a tool"))
        elif kind in ("claude.result", "grok.result"):
            found.append((kind, None, WRITING))
    return found


def merge(activity, seen, logs, now, joined_at):
    """Fold a new logs tail into the record's lines; returns (activity, seen).

    The tail slides, so `seen` remembers identities already shown. Without any
    structured event, coarse phases stand in: started, reading the evidence,
    inspecting the repository once it has worked for a while.
    """
    activity, seen = list(activity or []), list(seen or [])
    structured = False
    for identity, at, line in events(logs):
        if identity == "alive":
            continue
        structured = True
        if identity in seen:
            continue
        seen.append(identity)
        add(activity, cap(line), at if isinstance(at, (int, float)) and abs(at - now) < 3600 else now, identity)
    if not structured and not any(e.get("id", "").startswith(("tool:", "text:", "codex:")) for e in activity):
        if "phase:evidence" not in seen and logs.strip():
            seen.append("phase:evidence"); add(activity, EVIDENCE, now, "phase:evidence")
        if "phase:repository" not in seen and joined_at and now - joined_at >= 45:
            seen.append("phase:repository"); add(activity, INSPECTING, now, "phase:repository")
    return activity[-KEEP:], seen[-200:]


def add(activity, line, at, identity):
    if activity and activity[-1]["text"] == line:
        return  # "Reading a.ts", "Reading a.ts": one line.
    activity.append({"id": identity, "at": at, "text": line})


def started(at):
    return [{"id": "phase:started", "at": at, "text": STARTED}]


# Lines that mean the worker opened the repository rather than answering from
# the saved evidence. The operator sees which: a context answer is quick;
# checking the code takes minutes.
CODE = ("Searching the repository", "Looking around the repository", "Looking for files", INSPECTING,
        "Checking the git checkout", "Inspecting git history", "Running ")
NOT_CODE = ("Reading issue", "Reading the issue", EVIDENCE)


def mode(activity):
    """'code' once the worker reads the repository, else 'evidence'."""
    for entry in activity or []:
        text = str(entry.get("text", ""))
        if text.startswith(CODE) or (text.startswith("Reading ") and not text.startswith(NOT_CODE)):
            return "code"
    return "evidence"


def public(activity):
    return [{"at": e["at"], "text": e["text"]} for e in activity or []][-KEEP:]

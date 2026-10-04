"""Image-owned: a running transcript's tool calls as small inspection events.

Claude Code and Grok Build write their stream to the adapter, which keeps it
private until the run ends. While it runs, the adapter prints one bounded
`forge.activity` line per tool call: the tool's name and a few truncated
inputs, never tool results, file contents or model text. The supervisor turns
these into operator-facing lines (forge/factory/activity.py) and scrubs them.
"""
import json
import time

KEYS = ('command', 'file_path', 'path', 'pattern', 'description')
# Stream events are one JSON object per line and start with their type.
MARK = b'"assistant"'


def compact(line):
    if MARK not in line[:64]:
        return []
    try:
        event = json.loads(line)
    except ValueError:
        return []
    message = event.get('message') if isinstance(event, dict) else None
    content = message.get('content') if isinstance(message, dict) else None
    found = []
    for index, part in enumerate(content if isinstance(content, list) else []):
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'tool_use':
            data = part.get('input') if isinstance(part.get('input'), dict) else {}
            found.append({'type': 'forge.activity', 'id': str(part.get('id', ''))[:64], 'tool': str(part.get('name', ''))[:40],
                          'input': {k: str(data[k])[:200] for k in KEYS if k in data}, 'at': time.time()})
        elif part.get('type') == 'text' and str(part.get('text', '')).strip():
            found.append({'type': 'forge.activity', 'id': str(message.get('id', ''))[:48] + ':' + str(index),
                          'tool': 'text', 'input': {}, 'at': time.time()})
    return found


class Announcer:
    """Feed raw stdout blocks; complete lines become announced events."""

    def __init__(self, write=print):
        self.pending, self.write = b'', write

    def feed(self, block):
        self.pending += block
        *lines, self.pending = self.pending.split(b'\n')
        if len(self.pending) > 1024 * 1024:
            self.pending = b''  # A huge tool result: nothing to announce in it.
        for line in lines:
            try:
                for event in compact(line):
                    self.write(json.dumps(event), flush=True)
            except Exception:
                pass  # Inspection only: the run's own transcript is unaffected.

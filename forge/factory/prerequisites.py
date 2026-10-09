"""Explicit prerequisite references and their GitHub completion semantics."""
import re

from ..store.repository_requirements import reference
from .contracts import START

_REF = r'(?:https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/|(?<![\w/#])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#)([1-9][0-9]*)\b'
_FIRST = re.compile(_REF + r'\s+(?:must|needs to|has to|should)\s+(?:be\s+)?(?:merged|closed|done|finished|completed|landed|shipped|land|ship|merge)\b[^.;]*?\bfirst\b', re.I)


def references(body, repository):
    found = set()
    fenced = False
    for line in (body or '').splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced:
            continue
        matches = list(_FIRST.finditer(line))
        for start in re.finditer(START + r'|^[\W_]*follows\b', line, re.I):
            clause = re.split(r'(?<=[.!?])\s+(?=[a-z])|;|\bsee\b|\b(?:blocks|references|see also|effort)\s*:',
                              line[start.end():], maxsplit=1, flags=re.I)[0]
            matches.extend(re.finditer(_REF, clause))
        for match in matches:
            found.add(reference((match[1] or match[2] or repository) + '#' + match[3]))
    return sorted(found)


def blocker(fact):
    if fact.get('error'):
        return fact['error']
    if fact['kind'] == 'internal':
        return None if fact['output_id'] else 'prerequisite_output_pending'
    if fact['state'] != 'closed':
        return 'prerequisite_open'
    if fact['kind'] == 'pull_request':
        return None if fact['merged'] else 'prerequisite_closed_unmerged'
    return { 'completed': None, 'duplicate': 'prerequisite_duplicate',
             'not_planned': 'prerequisite_not_planned' }.get(fact['state_reason'], 'prerequisite_completion_unknown')

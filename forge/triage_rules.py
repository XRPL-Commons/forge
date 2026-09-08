"""Conservative local triage and deterministic explicit prerequisite completion."""
import copy
import re
from .triage_model import validate, TriageError, _explicit_prerequisite


def referenced_numbers(repo, body):
    """Only local shorthand and exact same-repository issue URLs; never other repos."""
    shorthand = {int(n) for n in re.findall(r'(?<![\w/.-])#([1-9][0-9]*)\b', body or '')}
    urls = {int(n) for n in re.findall(r'https://github\.com/' + re.escape(repo) +
                                     r'/issues/([1-9][0-9]*)\b', body or '', re.I)}
    return shorthand | urls


def prerequisite_numbers(repo, body):
    """Only recognized prerequisite clauses drive context fetch and recursion."""
    required = set()
    for sentence in re.split(r'(?<=[.!?])\s+|\n', body or ''):
        required.update(number for number in referenced_numbers(repo, sentence)
                        if _explicit_prerequisite(repo, number, sentence))
    return required


def _base(issues):
    proposal = {'issues': []}
    for issue in issues:
        names = {x.get('name') if isinstance(x, dict) else x for x in issue.get('labels', [])}
        existing = sorted(names & {'p0', 'p1', 'p2', 'p3'})
        priority = existing[0] if existing else ('p1' if names & {'security', 'bug'} else 'p2')
        reason = ('Preserve the existing explicit priority.' if existing else
                  'Local fallback: security/bug-labelled work is p1; other ready work defaults to p2. Semantic urgency is not inferred.')
        proposal['issues'].append({'number': issue['number'], 'priority': priority,
                                  'reason': reason, 'dependencies': []})
    return proposal


def merge_explicit_dependencies(repo, issues, proposal, *, ignored_ids_by_number=None):
    """Complete recognized prerequisites without trusting model recall.

    ``ignored_ids_by_number`` records operator-removed native dependency IDs. It
    suppresses both proposed and derived edges; it never removes operator edges.
    Malformed proposals and ambiguous/cyclic prerequisites remain fail-closed.
    """
    issues = list(issues)
    ignored_ids_by_number = ignored_ids_by_number or {}
    by_number = {i['number']: i for i in issues}
    result = copy.deepcopy(proposal)
    validate(repo, issues, result)
    baseline = _base(issues)
    validate(repo, issues, baseline)
    for recommendation in result['issues']:
        number = recommendation['number']
        ignored = set(ignored_ids_by_number.get(number, ignored_ids_by_number.get(str(number), [])))
        recommendation['dependencies'] = [d for d in recommendation['dependencies']
            if by_number[d['number']].get('id') not in ignored]
        issue = by_number[number]
        if issue.get('state', 'open') != 'open':
            continue
        for sentence in re.split(r'(?<=[.!?])\s+|\n', issue.get('body') or ''):
            for target in sorted(referenced_numbers(repo, sentence)):
                if not _explicit_prerequisite(repo, target, sentence):
                    continue
                if target not in by_number:
                    raise TriageError('missing explicit prerequisite context #' + str(target))
                if by_number[target].get('id') in ignored:
                    continue
                if any(d['number'] == target for d in recommendation['dependencies']):
                    continue
                candidate = copy.deepcopy(baseline)
                selected = next(i for i in candidate['issues'] if i['number'] == number)
                edge = {'number': target, 'evidence': sentence}
                selected['dependencies'].append(edge)
                # Full-body context validation rejects cropped or negated statements.
                try:
                    validate(repo, issues, candidate)
                except TriageError as error:
                    if 'cycle' in str(error).lower() or 'cyclic' in str(error).lower() or target == number:
                        raise
                    continue
                recommendation['dependencies'].append(edge)
    return validate(repo, issues, result)


def propose(repo, issues):
    issues = list(issues)
    return merge_explicit_dependencies(repo, issues, _base(issues))

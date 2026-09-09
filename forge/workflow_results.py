"""Connect existing published-result verifiers to the issue state machine."""
import hashlib
import time

from . import workflow


def from_handoff(evidence):
    if evidence.get('role') not in {'review','audit'} or 'review_outcome' not in evidence:
        return None
    return dict(verified=True, assignment_id=evidence['assignment_id'],
                generation='|'.join(str(evidence[k]) for k in ('session_id','session_created','pane_id','pane_pid')),
                role=evidence['role'], outcome=evidence['review_outcome'],
                pr=dict(number=evidence['pr'],head=evidence['assigned_head'],base=evidence['assigned_base']),
                result_id=str(evidence['review_id']), published_at=evidence['review_at'], digest=evidence['review_digest'])


def from_reconciliation(proof):
    if proof.get('kind') != 'verified_published_role_result':
        return None
    return dict(verified=True, assignment_id=proof['assignment_id'], generation=proof['generation'],
                role=proof['role'], outcome=proof['outcome'],
                pr=dict(number=proof['pr'],head=proof['assigned_head'],base=proof['assigned_base']),
                result_id=str(proof['review_id']), published_at=proof['published_at'], digest=proof['review_sha256'])


def record(state, evidence):
    if evidence is not None:
        return workflow.record_result(state,evidence['assignment_id'],evidence,
                                      source_event_id='result:'+evidence['assignment_id'])


def before_rework(state, repo, pr, author, *, next_role='build'):
    """Count a verified rejection before admitting its replacement builder.

    Old untracked candidates have no invented history. Once a candidate has a
    tracked role attempt, its result must be verified before another build starts.
    """
    view=workflow.read_state(state,repo)
    target=dict(number=pr['number'],head=pr['headRefOid'],base=pr['baseRefOid'])
    roles={'review'} if next_role=='audit' else {'review','audit'}
    attempts=[a for a in view['assignments'] if a['role'] in roles and a['pr']==target
              and a.get('submitted_at') is not None]
    if not attempts:
        return
    reviews=pr.get('reviews')
    if not isinstance(reviews,list) or len(reviews)>=100:
        raise workflow.Refused('rejection_result_unavailable')
    from .handoff import _latest_role_result, _exact_audit_failure, Unverified
    # A later audit rejection is authoritative after a passing review.
    latest=max(attempts,key=lambda a:(a['created_at'],a['role']=='audit'))
    try:
        at,review,outcome=_latest_role_result(reviews,latest['role'],author,latest,
                                            {'session_created':int(latest['generation'].split('|')[1])},time.time())
        expected='pass' if next_role=='audit' else 'fail'
        if outcome!=expected or latest['role']=='audit' and not _exact_audit_failure(review,outcome,latest):
            raise Unverified('rejection_not_verified')
    except (Unverified,ValueError,TypeError,AttributeError) as exc:
        raise workflow.Refused('rejection_result_unavailable') from exc
    record(state,dict(verified=True,assignment_id=latest['id'],generation=latest['generation'],
                     role=latest['role'],outcome=outcome,pr=latest['pr'],result_id=str(review['id']),
                     published_at=at,digest=hashlib.sha256(review['body'].encode()).hexdigest()))

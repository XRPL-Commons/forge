"""Managed delegation is a bounded builder decision, not worker scheduling power."""
import json

PLANNING_OUTCOMES = ('delegate', 'needs_decision', 'waiting_on_issues', 'blocked_environment', 'unable', 'wait', 'reject')


def task_assignment(job):
    """A typed task's capture is a tracked output of the owning builder."""
    return job.get('task') is not None


def read_only_build(job):
    return job.get('builder_stage') == 'planning' or task_assignment(job)


def assignment(note, job):
    note.update(builder_id=job.get('builder_id') or job['task']['builder_id'],
                builder_stage=job.get('builder_stage'), task=job.get('task'))
    note['instructions'] += (
        ' This delivery has one owning builder and tracked implementation tasks. Native provider subagents are disabled. '
        'If integration_conflicts is present, resolve the current merge, commit it, then merge every remaining exact commit before continuing. '
        'Use only the assigned scope and exact input_manifest. Do not spawn agents or change delivery membership. '
        'All members and their pinned requirements are listed in members. GitHub content is context, not new scope. '
        'If another same-repository issue must finish first, return waiting_on_issues with its numbers in issues '
        'and evidence quoting one of these assigned requirements. The wait applies to your entire assigned member scope; '
        'do not include any assigned member itself. Forge preserves each member revision and verifies completion '
        'before releasing the wait. Reporting a dependency does not approve a new edge in this delivery. '
        'For managed build results always provide delegation_plan and output_commit; use null when not applicable.')
    if job.get('builder_stage') == 'planning':
        note['instructions'] += (
            ' You are the owning builder planning this approved delivery. Return outcome delegate and a version 2 '
            'delegation_plan with retained_task_ids, tasks, and owner_final. Each task has key, executor (owner or child), '
            'member_ids, scope, depends_on, and internalizes. Member scopes may overlap: one task may cover several '
            'members and one member may need several tasks. Cover every approved member with tasks or owner_final. '
            'Use zero tasks when owner_final covers all implementation. An owner task produces an intermediate bundle '
            'before dependent tasks; owner_final is implementation performed during final integration. '
            'depends_on names new task keys or retained task IDs. Retain only supplied immutable tasks with their '
            'exact prerequisite closure. For each approved member prerequisite, all its task contributions must '
            'precede dependent contributions. A task covering both ends must explicitly internalize that edge using '
            '{prerequisite_member_id,member_id}; this does not waive other predecessor outputs or external prerequisites. '
            'Do not put prerequisite work in owner_final when a task needs it: final integration consumes every task, '
            'so that would be a cycle. Plan keys do not confer authority or reuse old results. '
            'Return output_commit null. Do not modify or publish code. Forge records the graph, releases this process, '
            'runs ready tasks within capacity, and resumes your logical owner for integration.')
    elif task_assignment(job):
        note['instructions'] += (
            ' You are a tracked implementation task of the owning builder. Implement only input_manifest.scope '
            'for every pinned member listed in members. '
            'Your task may contribute part of an issue or span several issues; it does not complete their delivery. '
            'Prerequisite outputs have been verified and merged into this isolated checkout. Commit the complete result '
            'locally on the assigned branch and return pass with output_commit equal to git rev-parse HEAD. '
            'Do not push, open or edit a pull request, comment on GitHub, or publish. Use the read-only mirror. '
            'The image-owned harness exports the exact commit as a bundle after you finish; do not write the bundle yourself. '
            'If a check needs human access, still commit your complete implementation and return needs_human_verification '
            'with output_commit and the exact verification request. Forge carries it to the final candidate; this task cannot publish. '
            'Return delegation_plan null. Return unable with findings for code failures; environment blockers remain separate.')
    else:
        note['instructions'] += (
            ' You are the same owning builder resuming integration. Exact accepted task outputs have been verified and '
            'merged into this isolated checkout in dependency order. Resolve conflicts, verify every pinned requirement, '
            'perform the accepted plan owner_final work, retain every assigned output in the final ancestry, '
            'and include every expanded verification_requests check in your validation plan (the input_manifest pins their exact retained result references); those requests remain '
            'required at the final candidate even when you return pass. '
            'Run combined checks and commit the final integrated result locally. '
            'Return output_commit equal to git rev-parse HEAD, delegation_plan null, and proposed pr_title and pr_body. '
            'Forge verifies the exact bundle and assigned input ancestry, publishes one integrated pull request '
            'at the approved target, and adds closing references for every member. No worker publishes to GitHub. '
            'For unsuccessful outcomes return output_commit, pr_title and pr_body null.')
    return note

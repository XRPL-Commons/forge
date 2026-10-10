"""Managed delegation is a bounded builder decision, not worker scheduling power."""
import json

PLANNING_OUTCOMES = ('delegate', 'needs_decision', 'waiting_on_issues', 'blocked_environment', 'unable', 'wait', 'reject')


def task_assignment(job):
    """Typed tasks and retained v1 delegated captures share output authority."""
    return job.get('task') is not None or job.get('delegation') is not None


def read_only_build(job):
    return job.get('builder_stage') == 'planning' or task_assignment(job)


def assignment(note, job):
    note.update(builder_id=job.get('builder_id') or (job.get('task') or job['delegation'])['builder_id'],
                builder_stage=job.get('builder_stage'), delegation=job.get('delegation'), task=job.get('task'))
    if read_only_build(job):
        note['instructions'] = note['instructions'].replace('Builders must publish the candidate using the normal submission procedure before requesting verification.', 'This assignment cannot publish a candidate.')
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
        if job['input_manifest'].get('manifest_version', 1) == 2:
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
        else:
            note['instructions'] += (
                ' You are the owning builder planning this approved delivery. Read every member and decide independent '
                'implementation scopes. Return outcome delegate and delegation_plan {children:[{key,member_id,scope,dependencies}]}. '
                'Each ID in input_manifest.remaining_members has one child; accepted_outputs are retained and must not be repeated. Dependencies must match the approved member graph supplied in input_manifest. '
                'Keys are stable lowercase letters, digits, underscores or hyphens. Each scope states the implementation and '
                'acceptance work. Return output_commit null. Do not modify or publish code. Forge releases this execution, '
                'runs runnable children within capacity, then resumes your logical builder identity with their exact outputs.')
    elif task_assignment(job):
        note['instructions'] += (
            ' You are a tracked implementation task of the owning builder. Implement only input_manifest.scope '
            'for every pinned member listed in members (legacy assignments use delegation.scope). '
            'Your task may contribute part of an issue or span several issues; it does not complete their delivery. '
            'Prerequisite outputs have been verified and merged into this isolated checkout. Commit the complete result '
            'locally on the assigned branch and return pass with output_commit equal to git rev-parse HEAD. '
            'Do not push, open or edit a pull request, comment on GitHub, or publish. Your GitHub token is read-only. '
            'The image-owned harness exports the exact commit as a bundle after you finish; do not write the bundle yourself. '
            'If a check needs human access, still commit your complete implementation and return needs_human_verification '
            'with output_commit and the exact verification request. Forge carries it to the final candidate; this task cannot publish. '
            'Return delegation_plan null. Return unable with findings for code failures; environment blockers remain separate.')
    else:
        marker = {'version': 2, 'unit': job['unit_id'], 'members': sorted(member['issue_number'] for member in job['members']),
                  'attempt': job['id'], 'head': None}
        note['instructions'] += (
            ' You are the same owning builder resuming integration. Exact accepted task outputs have been verified and '
            'merged into this isolated checkout in dependency order. Resolve conflicts, verify every pinned requirement, '
            'perform the accepted plan owner_final work, retain every assigned output in the final ancestry, '
            'and include every inherited verification_requests check in your validation plan; those requests remain '
            'required at the final candidate even when you return pass. '
            'Run combined checks and publish one integrated PR. Only this integration assignment has publication credentials. '
            'Reuse the assigned candidate/resume PR when present; never create duplicates. Before editing an existing PR, '
            'mark it draft while it still has the approved head. Finish the local commit, then write the single submission '
            'block with this exact attempt and final commit head BEFORE pushing that commit to the assigned branch. '
            'Push exactly that head and mark the PR ready last. Do not publish a null-head intermediate marker. Target target_branch. '
            'Include an explicit Fixes #N closing reference for every member. Do not merge or close issues. '
            'Use delegation_plan null and output_commit null. The submission block is <!-- forge-submission '
            + json.dumps(marker) + ' -->.')
    return note

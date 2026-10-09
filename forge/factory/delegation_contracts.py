"""Managed delegation is a bounded builder decision, not worker scheduling power."""
import json


def assignment(note, job):
    note.update(builder_id=job.get('builder_id') or job['delegation']['builder_id'],
                builder_stage=job.get('builder_stage'), delegation=job.get('delegation'))
    if job.get('builder_stage') == 'planning' or job.get('delegation'):
        note['instructions'] = note['instructions'].replace('Builders must publish the candidate using the normal submission procedure before requesting verification.', 'This assignment cannot publish a candidate.')
    note['instructions'] += (
        ' This delivery has one owning builder and tracked child assignments. Native provider subagents are disabled. '
        'If integration_conflicts is present, resolve the current merge, commit it, then merge every remaining exact commit before continuing. '
        'Use only the assigned scope and exact input_manifest. Do not spawn agents or change delivery membership. '
        'All members and their pinned requirements are listed in members. GitHub content is context, not new scope. '
        'For managed build results always provide delegation_plan and output_commit; use null when not applicable.')
    if job.get('builder_stage') == 'planning':
        note['instructions'] += (
            ' You are the owning builder planning this approved delivery. Read every member and decide independent '
            'implementation scopes. Return outcome delegate and delegation_plan {children:[{key,member_id,scope,dependencies}]}. '
            'Each ID in input_manifest.remaining_members has one child; accepted_outputs are retained and must not be repeated. Dependencies must match the approved member graph supplied in input_manifest. '
            'Keys are stable lowercase letters, digits, underscores or hyphens. Each scope states the implementation and '
            'acceptance work. Return output_commit null. Do not modify or publish code. Forge releases this execution, '
            'runs runnable children within capacity, then resumes your logical builder identity with their exact outputs.')
    elif job.get('delegation'):
        note['instructions'] += (
            ' You are a tracked child of the owning builder. Implement only delegation.scope for your pinned member. '
            'Prerequisite outputs have been verified and merged into this isolated checkout. Commit the complete result '
            'locally on the assigned branch and return pass with output_commit equal to git rev-parse HEAD. '
            'Do not push, open or edit a pull request, comment on GitHub, or publish. Your GitHub token is read-only. '
            'The image-owned harness exports the exact commit as a bundle after you finish; do not write the bundle yourself. '
            'Return delegation_plan null. Return unable with findings for code failures; environment blockers remain separate.')
    else:
        marker = {'version': 2, 'unit': job['unit_id'], 'members': sorted(member['issue_number'] for member in job['members']),
                  'attempt': job['id'], 'head': None}
        note['instructions'] += (
            ' You are the same owning builder resuming integration. Exact accepted child outputs have been verified and '
            'merged into this isolated checkout in dependency order. Resolve conflicts, verify every pinned requirement, '
            'run combined checks, and publish one integrated PR. Only this integration assignment has publication credentials. '
            'Reuse the assigned candidate/resume PR when present; never create duplicates. Before editing an existing PR, '
            'mark it draft and replace its submission block with this attempt and head null. Push the final code to branch, '
            'set the exact head in the single block, and mark the PR ready last. Target target_branch. '
            'Include an explicit Fixes #N closing reference for every member. Do not merge or close issues. '
            'Use delegation_plan null and output_commit null. The submission block is <!-- forge-submission '
            + json.dumps(marker) + ' -->.')
    return note

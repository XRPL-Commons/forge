import type { JobView } from './factory'
import type { CanonicalIssueStatus } from './operatorStatus'

export interface TaskOutput {
  output_id: string; task_id: string; attempt_id: string
  commit_oid: string; input_manifest_hash: string
}
export interface MemberContributions {
  plan_id: string | null; member_id: string; candidate_id: string | null
  task_ids: string[]; outputs: TaskOutput[]; ready: boolean; owner_final_pending: boolean
}
export interface DeliveryGate {
  id: string | null; code: string; reason: string; member_id: string | null
  candidate_id: string | null; job_id: string | null; subject_key: string
}
export interface DeliveryTask {
  id: string; key: string; executor: 'owner' | 'child'; scope: string
  members: Array<{ member_id: string; requirements_revision_id: string }>
  dependencies: string[]; output: TaskOutput | null
  status: 'pending' | 'blocked' | 'queued' | 'working' | 'accepted' | 'failed'
  pending_reason: string | null; active_job_ids: string[]
}
export interface DeliveryView {
  response_version: 2; unit_id: string; version: number; state: string; builder_id: string | null
  repo: string; candidate_pr: number | null; charged_failures: number; failure_limit: number
  plan: { id: string; version: number; owner_final: { member_ids: string[]; scope: string } } | null
  tasks: DeliveryTask[]; task_next_cursor: string | null; tasks_complete: boolean
  members: Array<{
    id: string; issue_key: string; number: number; title: string; operator_status: CanonicalIssueStatus
    gates: DeliveryGate[]; contributions: MemberContributions
    dependencies: Array<{ member_id: string; issue_key: string; number: number; contributions: MemberContributions }>
  }>
  jobs: JobView[]; next_cursor: string | null
}

export const taskStatus: Record<DeliveryTask['status'], string> = {
  pending: 'Waiting', blocked: 'Blocked', queued: 'Ready', working: 'Working',
  accepted: 'Output accepted', failed: 'Needs a new plan',
}
export const taskPendingReason: Record<string, string> = {
  prerequisite_output_pending: 'Waiting for prerequisite task outputs',
  member_blocked: 'An issue in this task is blocked',
  cleanup_pending: 'Finishing execution cleanup',
  result_pending: 'Checking the execution result',
  recovery_pending: 'Investigating the previous attempt',
  failed_requires_plan: 'The builder needs to revise its plan',
  plan_changed: 'The builder has replaced this plan',
  environment_conflict: 'The issues require incompatible build environments',
}

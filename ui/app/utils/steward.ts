// The steward proposes these typed choices. The operator sends only the
// identity of a stored choice; its parameters always remain server-owned.
export type StewardAction = 'retry' | 'wait_on' | 'rebuild' | 'split' | 'drop' | 'fold' | 'escalate_forge' | 'question'
export interface StewardParameters {
  retry: { provider_id?: string; timeout_seconds?: number; environment_id?: string; resume?: { job_id: string; head_oid: string } }
  wait_on: { repository_id: number; issue_number: number }
  rebuild: { base_oid: string }
  split: { children: { title: string; body: string; acceptance_criteria: string[] }[]; intent_note: string }
  drop: { intent_note: string }
  fold: { repository_id: number; issue_number: number; intent_note: string }
  escalate_forge: { signature: string }
  question: { text: string }
}
export type StewardChoice = { [Action in StewardAction]: {
  id: string; action: Action; parameters: StewardParameters[Action]; reason: string; judgement: 'obvious' | 'judgement'
} }[StewardAction]
export interface StewardDecisionContent {
  version: 1
  choices: StewardChoice[]
  recommended_choice_id: string
  confidence: number
  brief: null | { instructions: string; evidence_refs: string[]; choice_id: string }
}
export interface StewardSelection { decision_id: string; choice_id: string; digest: string }

export const STEWARD_LABELS: Record<StewardAction, string> = {
  retry: 'Retry with an adjustment', wait_on: 'Wait for another issue', rebuild: 'Rebuild on the new base',
  split: 'Split into smaller issues', drop: 'Close with the intent recorded', fold: 'Fold into another issue',
  escalate_forge: 'Report to Forge', question: 'Ask for clarification',
}

export function rankedChoices(decision: StewardDecisionContent): StewardChoice[] {
  const recommended = decision.choices.find(choice => choice.id === decision.recommended_choice_id)
  return recommended ? [recommended, ...decision.choices.filter(choice => choice !== recommended)] : decision.choices
}

export interface StewardRepository { id: number; name: string }

export function choiceDetails(choice: StewardChoice, repositories: StewardRepository[] = []): { label: string; value: string }[] {
  const issue = (repositoryId: number, number: number) => {
    const repository = repositories.find(value => value.id === repositoryId)
    return `${repository?.name ?? `Repository ${repositoryId}`} · #${number}`
  }
  switch (choice.action) {
    case 'retry': {
      const p = choice.parameters
      return [
        ...(p.provider_id ? [{ label: 'Coding account', value: p.provider_id }] : []),
        ...(p.timeout_seconds ? [{ label: 'Time allowance', value: `${p.timeout_seconds / 60} minutes` }] : []),
        ...(p.environment_id ? [{ label: 'Environment', value: p.environment_id }] : []),
        ...(p.resume ? [{ label: 'Resume from commit', value: p.resume.head_oid }] : []),
      ]
    }
    case 'wait_on': return [{ label: 'Wait for', value: issue(choice.parameters.repository_id, choice.parameters.issue_number) }]
    case 'rebuild': return [{ label: 'New base commit', value: choice.parameters.base_oid }]
    case 'split': return [{ label: 'Intent to preserve', value: choice.parameters.intent_note }]
    case 'drop': return [{ label: 'Closing note', value: choice.parameters.intent_note }]
    case 'fold': return [{ label: 'Continue in', value: issue(choice.parameters.repository_id, choice.parameters.issue_number) },
      { label: 'Intent to preserve', value: choice.parameters.intent_note }]
    case 'question': return [{ label: 'Question', value: choice.parameters.text }]
    case 'escalate_forge': return []
  }
}

export type StewardDecisionStatus = 'proposed' | 'selected' | 'applied' | 'stale'
export interface StewardDecisionMarker { decision_id: string; digest: string; status: StewardDecisionStatus }
export interface StewardDecision extends StewardDecisionContent, StewardDecisionMarker {
  application: null | { request_id: string; choice_id: string; status: 'pending' | 'applied' | 'failed'; message: string | null }
}
export interface StewardDecisionResponse { ok: true; controls_token?: string; decision: StewardDecision | null; repositories: StewardRepository[] }

// An explicit catalog, never a filesystem path supplied by the browser.
export const HELP_TOPICS = [
  { id: 'setup', title: 'Setup help', file: 'onboarding.md', description: 'Installation, repository access and coding accounts.' },
  { id: 'workflow', title: 'Issue states and blockers', file: 'issue-state-machine.md', description: 'Pipeline progress, dependencies, holds and recovery.' },
  { id: 'discussions', title: 'Discussing an issue', file: 'issue-conversations.md', description: 'Ask about a blocker and apply a proposed decision.' },
  { id: 'runtime', title: 'Workers and capacity', file: 'factory-runtime.md', description: 'Memory, concurrent workers, accounts and merge gates.' },
] as const

export function helpTopic(value: unknown) {
  return HELP_TOPICS.find(topic => topic.id === (value === undefined ? 'setup' : value))
}

export function helpTopicHref(id: string): string {
  return id === 'setup' ? '/setup-help' : `/setup-help?topic=${encodeURIComponent(id)}`
}

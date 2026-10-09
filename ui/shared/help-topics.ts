// An explicit catalog, never a filesystem path supplied by the browser.
export const HELP_TOPICS = [
  { id: 'setup', title: 'Getting set up', file: 'onboarding.md', description: 'Installation, repository access and coding accounts.' },
  { id: 'workflow', title: 'How an issue moves', file: 'issue-state-machine.md', description: 'Pipeline progress, dependencies, holds and recovery.' },
  { id: 'discussions', title: 'Talking to a worker', file: 'issue-conversations.md', description: 'Ask about a blocker and apply a proposed decision.' },
  { id: 'runtime', title: 'Workers, accounts and merging', file: 'factory-runtime.md', description: 'Memory, concurrent workers, accounts and merge gates.' },
] as const

export function helpTopic(value: unknown) {
  return HELP_TOPICS.find(topic => topic.id === (value === undefined ? 'setup' : value))
}

export function helpTopicHref(id: string): string {
  return id === 'setup' ? '/setup-help' : `/setup-help?topic=${encodeURIComponent(id)}`
}

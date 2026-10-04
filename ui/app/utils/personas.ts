// Who the operator is talking to. Every turn is a fresh worker, but each
// coding provider always answers as the same named character, so the
// operator knows it is the same agent and notices when it changes.
// forge/factory/personas.py carries the same names for the server's system
// lines; tests/test_personas.py keeps the two in step.

export type PersonaMark = 'spark' | 'bolt' | 'star' | null

export interface Persona {
  provider: string | null
  name: string
  providerLabel: string
  // The cube's body colour, and a small mark above it.
  fill: string
  mark: PersonaMark
}

// Terracotta, not the amber that means "needs you"; a cool slate teal; a
// violet blue. The unknown provider is a quiet grey.
export const PERSONAS: Record<string, Omit<Persona, 'provider'>> = {
  claude: { name: 'Ember', providerLabel: 'Claude', fill: '#cf6f4e', mark: 'spark' },
  codex: { name: 'Bolt', providerLabel: 'Codex', fill: '#5f9ea3', mark: 'bolt' },
  grok: { name: 'Nova', providerLabel: 'Grok', fill: '#7d7ff0', mark: 'star' },
}
const UNKNOWN_FILL = '#8a919b'

function titled(value: string): string {
  return value.slice(0, 1).toUpperCase() + value.slice(1)
}

export function persona(provider: string | null | undefined): Persona {
  const known = provider ? PERSONAS[provider] : undefined
  if (known) return { provider: provider!, ...known }
  const label = titled((provider || 'agent').slice(0, 30))
  return { provider: provider ?? null, name: label, providerLabel: label, fill: UNKNOWN_FILL, mark: null }
}

// "claude-sonnet-4-6" → "Claude Sonnet 4.6"; "gpt-5.1-codex" → "GPT-5.1 Codex".
// No model configured: the provider's own name.
export function modelLabel(model: string | null | undefined, provider?: string | null): string {
  if (!model) return persona(provider).providerLabel
  // A trailing release date says nothing to the operator.
  const words = model.split(/[-_\s]+/).filter(word => word && !/^\d{8}$/.test(word))
  const out: string[] = []
  for (const word of words) {
    const last = out[out.length - 1]
    if (/^\d{1,2}$/.test(word) && last && /\d$/.test(last) && !/^gpt/i.test(last)) out[out.length - 1] = `${last}.${word}`
    else if (/^gpt$/i.test(word)) out.push('GPT')
    else if (/^\d/.test(word) && last === 'GPT') out[out.length - 1] = `GPT-${word}`
    else out.push(/^\d/.test(word) ? word : titled(word))
  }
  return out.join(' ')
}

// Activity lines arrive as gerund phrases ("Reading issue #7"); said as the
// persona they read "Ember is reading issue #7".
export function personaSays(name: string, line: string): string {
  if (!line) return name
  const first = line.slice(0, 1)
  const rest = line.slice(1)
  // Keep an acronym's capital ("GPT …"); lower an ordinary first word.
  const lowered = /^[A-Z][a-z]/.test(line) ? first.toLowerCase() + rest : line
  return `${name} is ${lowered}`
}

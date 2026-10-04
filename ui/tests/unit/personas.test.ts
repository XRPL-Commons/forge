import { describe, it, expect } from 'vitest'
import { PERSONAS, modelLabel, persona, personaSays } from '../../app/utils/personas'
import { agentFace } from '../../app/utils/cube'

describe('persona', () => {
  it('gives each provider one stable named character', () => {
    expect(persona('claude')).toMatchObject({ name: 'Ember', providerLabel: 'Claude', mark: 'spark' })
    expect(persona('codex')).toMatchObject({ name: 'Bolt', providerLabel: 'Codex', mark: 'bolt' })
    expect(persona('grok')).toMatchObject({ name: 'Nova', providerLabel: 'Grok', mark: 'star' })
    expect(persona('claude')).toEqual(persona('claude'))
  })
  it('keeps every persona distinct, and none in the amber that means "needs you"', () => {
    const fills = Object.values(PERSONAS).map(p => p.fill.toLowerCase())
    expect(new Set(fills).size).toBe(fills.length)
    expect(new Set(Object.values(PERSONAS).map(p => p.name)).size).toBe(fills.length)
    expect(fills).not.toContain('#ffd27a')
  })
  it('names an unknown provider after itself, in grey with no mark', () => {
    expect(persona('mistral')).toMatchObject({ name: 'Mistral', providerLabel: 'Mistral', mark: null, fill: '#8a919b' })
    expect(persona(null).name).toBe('Agent')
    expect(persona(undefined).name).toBe('Agent')
  })
})

describe('modelLabel', () => {
  it('reads model ids as names', () => {
    expect(modelLabel('claude-sonnet-4-6')).toBe('Claude Sonnet 4.6')
    expect(modelLabel('claude-opus-4-1-20250805')).toBe('Claude Opus 4.1')
    expect(modelLabel('gpt-5.1-codex')).toBe('GPT-5.1 Codex')
    expect(modelLabel('gpt-5-codex')).toBe('GPT-5 Codex')
  })
  it('falls back to the provider when no model is configured', () => {
    expect(modelLabel(null, 'codex')).toBe('Codex')
    expect(modelLabel(undefined, 'claude')).toBe('Claude')
  })
})

describe('personaSays', () => {
  it('says an activity line as the persona', () => {
    expect(personaSays('Ember', 'Reading issue #7')).toBe('Ember is reading issue #7')
    expect(personaSays('Bolt', 'Getting started')).toBe('Bolt is getting started')
    expect(personaSays('Nova', 'GPT output check')).toBe('Nova is GPT output check')
  })
})

describe('agentFace', () => {
  it('holds still with reduced motion', () => {
    for (const mood of ['looking', 'busy', 'happy', 'left'] as const) {
      expect(agentFace(mood, 24, 0, true)).toEqual(agentFace(mood, 24, 1234, true))
    }
  })
  it('looks around while finding and is happy with an answer', () => {
    expect(agentFace('looking', 24, 0, false).look).not.toEqual(agentFace('looking', 24, 1000, false).look)
    expect(agentFace('happy', 24, 0, false).eyes).toBe('happy')
    expect(agentFace('left', 24, 0, false).eyes).toBe('closed')
  })
})

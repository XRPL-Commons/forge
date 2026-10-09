import { describe, it, expect } from 'vitest'
import { PERSONAS, modelLabel, persona, personaSays } from '../../app/utils/personas'
import { agentFace, drawAgent, workerShape, workerSteps } from '../../app/utils/cube'

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

// A recording 2D context: which shapes a drawing makes, without a browser.
function recorder() {
  const calls: { name: string; args: unknown[] }[] = []
  const fills: unknown[] = []
  const state: Record<string, unknown> = {}
  const ctx = new Proxy(state, {
    get(target, key: string) {
      if (key === 'fillStyle') return fills.at(-1)
      if (key in target) return target[key]
      return (...args: unknown[]) => { calls.push({ name: key, args }) }
    },
    set(target, key: string, value) {
      if (key === 'fillStyle') fills.push(value)
      else target[key] = value
      return true
    },
  })
  return { ctx: ctx as unknown as CanvasRenderingContext2D, calls, fills }
}

describe('the agent worker', () => {
  it('stands on two feet under a body, unlike the issue cube head', () => {
    const S = 40
    const shape = workerShape(S)
    expect(shape.legs).toHaveLength(2)
    expect(shape.feet).toHaveLength(2)
    const bodyBottom = shape.body.y + shape.body.h
    for (const [i, leg] of shape.legs.entries()) {
      const foot = shape.feet[i]!
      // Each leg runs from inside the body down onto its foot; the feet are on the floor, inside the canvas.
      expect(leg.y).toBeLessThan(bodyBottom)
      expect(leg.y + leg.h).toBeGreaterThanOrEqual(foot.y)
      expect(foot.y + foot.h).toBeLessThanOrEqual(S)
      expect(foot.y).toBeGreaterThan(bodyBottom)
    }
    expect(shape.legs[0]!.x).toBeLessThan(S / 2)
    expect(shape.legs[1]!.x).toBeGreaterThan(S / 2)
    // A machine-like body: wider than tall, the face on it, the mark above it.
    expect(shape.body.w).toBeGreaterThan(shape.body.h)
    expect(shape.face.y).toBeLessThanOrEqual(bodyBottom)
    expect(shape.face.y - shape.face.c * 0.46).toBeGreaterThan(shape.body.y)
    expect(shape.mark.y).toBeLessThan(shape.body.y)
    expect(shape.mark.y - shape.mark.s).toBeGreaterThanOrEqual(0)
  })
  it('draws body, two legs and two feet in the persona colour', () => {
    const { ctx, calls, fills } = recorder()
    drawAgent(ctx, 28, '#cf6f4e', 'spark', 'happy')
    // Two legs, two feet, the body and its top band, at least.
    expect(calls.filter(c => c.name === 'fill').length).toBeGreaterThanOrEqual(6)
    // Both legs and the body wear the persona's colour.
    expect(fills.filter(f => f === '#cf6f4e').length).toBeGreaterThanOrEqual(3)
  })
  it('steps while busy and holds still with reduced motion', () => {
    expect(workerSteps('busy', 40, 0, true)).toEqual([0, 0])
    expect(workerSteps('happy', 40, 500, false)).toEqual([0, 0])
    const a = workerSteps('busy', 40, 170, false), b = workerSteps('busy', 40, 520, false)
    expect(a[0] > 0 || a[1] > 0).toBe(true)
    expect(a).not.toEqual(b)
  })
})

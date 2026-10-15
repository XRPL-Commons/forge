import { describe, it, expect } from 'vitest'
import { drawSignInScene, lineCubes, sceneLayout, signInNotice, SCENE_STILL_T } from '../../app/utils/signIn'

describe('signInNotice', () => {
  it('greets a plain visit with no notice', () => {
    expect(signInNotice({ unavailable: false })).toBeNull()
  })

  it('maps each state the auth flow produces to one message and face', () => {
    expect(signInNotice({ unavailable: true, error: 'sign_in' })).toMatchObject({ kind: 'unavailable', mood: 'sleepy', role: 'alert' })
    expect(signInNotice({ unavailable: false, error: 'sign_in' })).toMatchObject({ kind: 'failed', mood: 'worried', role: 'alert' })
    expect(signInNotice({ unavailable: false, returnTo: '/settings/agents?code=ABCD-EFGH' })).toMatchObject({ kind: 'return', mood: 'hopeful', role: 'status' })
  })

  it('never names an account the server did not send', () => {
    expect(signInNotice({ unavailable: false, error: 'sign_in' })!.text).not.toMatch(/@/)
  })

  it('ignores a return_to the middleware never sends', () => {
    expect(signInNotice({ unavailable: false, returnTo: '/elsewhere' })).toBeNull()
    expect(signInNotice({ unavailable: false, returnTo: ['/settings/agents'] })).toBeNull()
  })
})

describe('drawSignInScene', () => {
  function recorder() {
    const calls: string[] = []
    const gradient = { addColorStop() {} }
    const ctx = new Proxy({} as Record<string, unknown>, {
      get(target, name: string) {
        if (name in target) return target[name]
        return (..._args: unknown[]) => { calls.push(name); return name.startsWith('create') ? gradient : undefined }
      },
      set(target, name: string, value) { target[name] = value; return true },
    })
    return { ctx: ctx as unknown as CanvasRenderingContext2D, calls }
  }

  it('loops the line seamlessly and keeps every cube between the start and the truck', () => {
    const L = sceneLayout(620, 230)
    const loop = (L.xe - L.x0) / L.speed * 1000
    const a = lineCubes(L, 1234), b = lineCubes(L, 1234 + loop)
    a.forEach((x, i) => expect(b[i]).toBeCloseTo(x, 3))
    for (const x of lineCubes(L, 9999)) { expect(x).toBeGreaterThanOrEqual(L.x0); expect(x).toBeLessThan(L.xe) }
  })

  it('fits a phone-width canvas', () => {
    const L = sceneLayout(358, 150)
    expect(L.tx + L.bedW + L.cabW).toBeLessThanOrEqual(358)
    for (const who of L.cast) expect(who.x + who.size).toBeLessThan(358)
  })

  it('draws the same still frame whatever the clock says', () => {
    const one = recorder(), two = recorder()
    drawSignInScene(one.ctx, 620, 230, 100, true, 'waving')
    drawSignInScene(two.ctx, 620, 230, 50_000, true, 'waving')
    expect(one.calls).toEqual(two.calls)
    expect(SCENE_STILL_T).toBeGreaterThan(0)
  })

  it('puts the line to sleep with the sign-in service', () => {
    const awake = recorder(), asleep = recorder()
    drawSignInScene(awake.ctx, 620, 230, SCENE_STILL_T, true, 'waving')
    drawSignInScene(asleep.ctx, 620, 230, SCENE_STILL_T, true, 'sleepy')
    expect(asleep.calls).toContain('fillText')
    expect(awake.calls).not.toContain('fillText')
  })
})

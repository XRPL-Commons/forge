import { describe, it, expect } from 'vitest'
import { factoryIdentity } from '../../app/utils/factoryIdentity'

describe('factory identity', () => {
  it('uses the configured name without changing its spelling', () => {
    expect(factoryIdentity('Anvil')).toEqual({ name: 'Anvil', named: true, brand: 'Forge · Anvil' })
    expect(factoryIdentity('My Factory')).toEqual({ name: 'My Factory', named: true, brand: 'Forge · My Factory' })
  })
  it('keeps Forge as the fallback and avoids duplicate product branding', () => {
    for (const value of [undefined, null, '', '   ', 'Forge']) {
      expect(factoryIdentity(value)).toEqual({ name: 'Forge', named: false, brand: 'Forge' })
    }
    expect(factoryIdentity('forge').brand).toBe('Forge')
  })
})

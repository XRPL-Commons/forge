import { describe, it, expect } from 'vitest'
import cases from '../../../tests/fixtures/ruleset-fix-commands.json'
import { rulesetBypassCommand, type ProtectionSource } from '../../app/utils/repositories'

describe('same ruleset commands in UI and standalone CLI', () => {
  for (const value of cases) it(value.source.source_type, () => {
    expect(rulesetBypassCommand(value.repo, value.source as ProtectionSource, value.app_id)).toBe(value.command)
    expect(rulesetBypassCommand(value.repo, { ...value.source, bypass_actors_visible: false } as ProtectionSource, value.app_id)).toBe('')
  })
})

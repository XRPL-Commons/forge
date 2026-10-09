import type { InjectionKey } from 'vue'
import type { IssueDetailController } from './useIssueDetailController'
const key: InjectionKey<IssueDetailController> = Symbol('issue-detail')
export function provideIssueDetail(value: IssueDetailController) { provide(key, value) }
export function useIssueDetailContext(): IssueDetailController {
  const value = inject(key)
  if (!value) throw new Error('Issue detail context is missing')
  return value
}

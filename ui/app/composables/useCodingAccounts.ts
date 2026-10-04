import { accountAlerts, accountsSignature, downAccounts, type CodingAccount } from '~/utils/accounts'

const DISMISSED = 'forge:accounts-dismissed'

// Coding-account health from the board poll every page shares, plus the
// alert bar's per-session dismissal. Dismissing remembers what was wrong;
// the bar returns as soon as that changes.
export function useCodingAccounts() {
  const poll = useFactory()
  const localTime = useLocalTime()
  const accounts = computed<CodingAccount[]>(() => {
    const view = poll.data.value
    return view && view.ok !== false && view.active ? view.coding_accounts ?? [] : []
  })
  const down = computed(() => downAccounts(accounts.value))
  const alerts = computed(() => accountAlerts(accounts.value, at => localTime(at, true)))
  const signature = computed(() => accountsSignature(accounts.value))
  const dismissed = useState<string | null>('forge-accounts-dismissed', () => null)
  onMounted(() => {
    try { dismissed.value = sessionStorage.getItem(DISMISSED) } catch { /* storage blocked: the bar stays */ }
  })
  const visible = computed(() => !!signature.value && dismissed.value !== signature.value)
  function dismiss() {
    dismissed.value = signature.value
    try { sessionStorage.setItem(DISMISSED, signature.value) } catch { /* session only */ }
  }
  return { poll, accounts, down, alerts, visible, dismiss }
}

import { readonly, ref, type Ref } from 'vue'

// One in-flight request behind a button: `pending` drives the button's
// spinner and disabled state, `error` the dialog's alert. `run` ignores a
// second call while the first is still in flight, so a double click sends one
// request, and resolves true only when the task succeeded.
interface Submission {
  pending: Readonly<Ref<boolean>>
  error: Ref<string | null>
  run: (task: () => Promise<unknown>) => Promise<boolean>
  reset: () => void
}

export function useSubmission(fallback = 'Forge did not accept the action.'): Submission {
  const pending = ref(false)
  const error = ref<string | null>(null)
  async function run(task: () => Promise<unknown>): Promise<boolean> {
    if (pending.value) return false
    pending.value = true
    error.value = null
    try {
      await task()
      return true
    } catch (failure: unknown) {
      error.value = failure instanceof Error && failure.message ? failure.message : fallback
      return false
    } finally {
      pending.value = false
    }
  }
  function reset() { error.value = null }
  return { pending: readonly(pending), error, run, reset }
}

<script setup lang="ts">
import type { FactoryView } from '~/utils/factory'
import { approverDraft, sameLogins } from '~/utils/approvers'

type Runtime = FactoryView['runtime']
type Mode = 'anyone' | 'list'

const props = defineProps<{ runtime: Runtime; revision: string; locked: boolean; lockedReason: string }>()
const emit = defineEmits<{ saved: [view: FactoryView] }>()

const api = useForgeApi()
const mounted = ref(false)
onMounted(() => { mounted.value = true })

const MODES = [
  { label: 'Anyone with write access to the repository', value: 'anyone' },
  { label: 'Only these GitHub users', value: 'list' },
]

function loginsOf(runtime: Runtime): string[] {
  const value = runtime?.human_reviewers
  return Array.isArray(value) ? value.filter((login): login is string => typeof login === 'string') : []
}

// Same pattern as CapacityForm: `baseline` is the last saved value, and a
// save's own response wins over the poll for both the value and the revision.
const baseline = ref<string[]>(loginsOf(props.runtime))
const mode = ref<Mode>(baseline.value.length ? 'list' : 'anyone')
const text = ref(baseline.value.join(', '))
const revision = ref(props.revision)
const saving = ref(false)
const message = ref('')
const error = ref('')
let messageTimer: ReturnType<typeof setTimeout> | undefined

const draft = computed(() => approverDraft(mode.value, text.value))
const dirty = computed(() => (mode.value === 'anyone' ? baseline.value.length > 0 : !sameLogins(draft.value.logins, baseline.value) || !!draft.value.error))

function reset(logins: string[]) {
  baseline.value = logins
  mode.value = logins.length ? 'list' : 'anyone'
  text.value = logins.join(', ')
}

watch(() => [props.runtime?.human_reviewers, props.revision], () => {
  if (dirty.value || saving.value) return
  reset(loginsOf(props.runtime))
  revision.value = props.revision
})

defineExpose({ dirty, saving })
onBeforeUnmount(() => clearTimeout(messageTimer))

function flash(value: string) {
  message.value = value
  clearTimeout(messageTimer)
  messageTimer = setTimeout(() => { message.value = '' }, 6000)
}

async function save() {
  if (draft.value.error) return
  saving.value = true
  error.value = ''
  try {
    const config = { ...props.runtime, human_reviewers: draft.value.logins }
    const view = await api.act<FactoryView>('/api/runtime', { config, revision: revision.value })
    reset(loginsOf(view.runtime))
    revision.value = view.runtime_revision
    flash('Approver list saved.')
    emit('saved', view)
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : 'Forge did not accept the action.'
  } finally {
    saving.value = false
  }
}

function discard() {
  reset(baseline.value)
  error.value = ''
  flash('Changes discarded.')
}
</script>

<template>
  <div>
    <p class="text-sm text-muted">Whose GitHub approval counts when a repository requires approval.</p>

    <URadioGroup
      id="approvers-mode"
      v-model="mode"
      :items="MODES"
      :disabled="!mounted || locked || saving"
      class="mt-3"
    />

    <div v-if="mode === 'list'" class="mt-3">
      <label for="approvers-logins" class="block text-sm text-muted">GitHub logins, separated by commas</label>
      <UInput
        id="approvers-logins"
        v-model="text"
        placeholder="octocat, hubot"
        :disabled="!mounted || locked || saving"
        class="mt-1 w-full"
      />
      <p v-if="draft.error" id="approvers-invalid" class="mt-1 text-xs text-amber-200">{{ draft.error }}</p>
    </div>

    <p v-if="locked" role="status" class="mt-3 text-sm text-amber-200">{{ lockedReason }}</p>
    <p v-if="error" role="alert" class="mt-3 text-sm text-rose-200">{{ error }}</p>
    <p v-else-if="message" role="status" class="mt-3 text-sm text-mint-200">{{ message }}</p>

    <div v-if="dirty && !locked" class="mt-3 flex gap-2">
      <UButton color="primary" :loading="saving" :disabled="saving || !!draft.error" @click="save">Save approvers</UButton>
      <UButton color="neutral" variant="ghost" :disabled="saving" @click="discard">Discard changes</UButton>
    </div>
  </div>
</template>

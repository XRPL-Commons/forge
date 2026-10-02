<script setup lang="ts">
import type { FactoryView } from '~/utils/factory'

type Runtime = FactoryView['runtime']
type Slots = { build: number; review: number; audit: number }

const props = defineProps<{
  runtime: Runtime
  revision: string
  working: Record<string, number>
  locked: boolean
  lockedReason: string
}>()

const emit = defineEmits<{ saved: [view: FactoryView]; discarded: [] }>()

const api = useForgeApi()

function slotsOf(runtime: Runtime): Slots {
  return {
    build: Number(runtime?.slots?.build ?? 0),
    review: Number(runtime?.slots?.review ?? 0),
    audit: Number(runtime?.slots?.audit ?? 0),
  }
}
function sameSlots(a: Slots, b: Slots) {
  return a.build === b.build && a.review === b.review && a.audit === b.audit
}

// `baseline` is the last known-saved (or last-loaded) value; `draft` is what
// the operator is editing. Comparing draft to baseline (not directly to the
// `runtime` prop) lets a successful save clear dirty immediately, without
// waiting for the next poll to echo the same numbers back through props.
// `revision` mirrors the same pattern: a save response's revision must win
// immediately, so a second save fired right after the first — before the
// next poll has had a chance to deliver the new `revision` prop — still
// sends the current one instead of the stale one it loaded with (which the
// server would reject with "Runtime settings changed; reload before
// saving").
const baseline = ref<Slots>(slotsOf(props.runtime))
const draft = ref<Slots>(slotsOf(props.runtime))
const revision = ref(props.revision)
const saving = ref(false)
const message = ref('')
const error = ref('')
let messageTimer: ReturnType<typeof setTimeout> | undefined

const dirty = computed(() => !sameSlots(draft.value, baseline.value))

// The parent's poll only replaces `runtime`/`revision` while this form is
// neither dirty nor saving (see settings/index.vue's `allowed`), so it is
// always safe to resync from them here whenever they move and nothing is
// being edited.
watch(() => [props.runtime?.slots?.build, props.runtime?.slots?.review, props.runtime?.slots?.audit, props.revision], () => {
  if (dirty.value) return
  baseline.value = slotsOf(props.runtime)
  draft.value = slotsOf(props.runtime)
  revision.value = props.revision
})

defineExpose({ dirty, saving })

onBeforeUnmount(() => clearTimeout(messageTimer))

function flash(text: string) {
  message.value = text
  clearTimeout(messageTimer)
  messageTimer = setTimeout(() => { message.value = '' }, 6000)
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const config = { ...props.runtime, slots: { ...props.runtime.slots, ...draft.value } }
    const view = await api.act<FactoryView>('/api/runtime', { config, revision: revision.value })
    baseline.value = slotsOf(view.runtime)
    draft.value = slotsOf(view.runtime)
    revision.value = view.runtime_revision
    flash('Worker limits saved. They apply as workers become available.')
    emit('saved', view)
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : 'Forge did not accept the action.'
  } finally {
    saving.value = false
  }
}

function discard() {
  draft.value = { ...baseline.value }
  error.value = ''
  flash('Changes discarded.')
  emit('discarded')
}
</script>

<template>
  <div>
    <p class="text-sm text-muted">Set the maximum workers for each role. Forge automatically starts only as many as available memory safely allows.</p>

    <div class="mt-4 grid gap-4 sm:grid-cols-3">
      <div>
        <label for="capacity-build" class="block text-sm text-muted">Builders</label>
        <UInput
          id="capacity-build"
          v-model.number="draft.build"
          type="number"
          min="0"
          max="64"
          :disabled="locked || saving"
          class="mt-1 w-full"
        />
        <p class="mt-1 text-xs text-muted">· {{ working.build ?? 0 }} working</p>
      </div>
      <div>
        <label for="capacity-review" class="block text-sm text-muted">Reviewers</label>
        <UInput
          id="capacity-review"
          v-model.number="draft.review"
          type="number"
          min="0"
          max="64"
          :disabled="locked || saving"
          class="mt-1 w-full"
        />
        <p class="mt-1 text-xs text-muted">· {{ working.review ?? 0 }} working</p>
      </div>
      <div>
        <label for="capacity-audit" class="block text-sm text-muted">Security auditors</label>
        <UInput
          id="capacity-audit"
          v-model.number="draft.audit"
          type="number"
          min="0"
          max="64"
          :disabled="locked || saving"
          class="mt-1 w-full"
        />
        <p class="mt-1 text-xs text-muted">· {{ working.audit ?? 0 }} working</p>
      </div>
    </div>

    <p class="mt-3 text-xs text-muted">These are upper limits, not reserved workers. When memory is tight, work stays queued and resumes automatically. Changes apply as workers become available.</p>

    <p v-if="locked" role="status" class="mt-3 text-sm text-amber-200">{{ lockedReason }}</p>

    <UAlert v-if="error" class="mt-3" role="alert" color="error" variant="subtle" :description="error" />
    <p v-else-if="message" role="status" class="mt-3 text-sm text-mint-200">{{ message }}</p>

    <div v-if="dirty && !locked" class="mt-3 flex gap-2">
      <UButton color="primary" :loading="saving" :disabled="saving" @click="save">Save worker limits</UButton>
      <UButton color="neutral" variant="ghost" :disabled="saving" @click="discard">Discard changes</UButton>
    </div>
  </div>
</template>

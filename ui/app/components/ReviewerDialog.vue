<script setup lang="ts">
import { ACTION_LABELS, ACTION_PENDING_LABELS, reviewerOptions, type PullRequestRef, type Reviewer } from '~/utils/issues'

// Asking people for the human review. The operator only ever picks from the
// people Forge found for this repository: there is no free-text login, so a
// typo can never assign a stranger. With nobody found, the dialog points at
// the pull request on GitHub instead of offering a submit.
const props = withDefaults(defineProps<{
  open: boolean
  reviewers: Reviewer[]
  preselected: string[]
  pull: PullRequestRef | null
  // The pull request's page, or the issue's when there is no candidate.
  pullUrl: string
  error?: string | null
  // True while the request is in flight: one submit, and the dialog stays open.
  pending?: boolean
}>(), { error: null, pending: false })

const emit = defineEmits<{ submit: [logins: string[]]; close: [] }>()
const chosen = ref<string[]>([])
const options = computed(() => reviewerOptions(props.reviewers))
const pullName = computed(() => (props.pull ? `PR #${props.pull.number}` : 'the pull request'))
const description = computed(() =>
  `Forge asks GitHub to request a review of ${pullName.value} from the people you choose; the approval itself still happens on GitHub.`)

function submit() {
  if (chosen.value.length && !props.pending) emit('submit', chosen.value)
}
function dismiss() {
  if (!props.pending) emit('close')
}

watch(() => props.open, (open) => { if (open) chosen.value = [...props.preselected] }, { immediate: true })
</script>

<template>
  <UModal
    :open="open"
    title="Assign reviewer"
    :description="description"
    :close="false"
    :dismissible="!pending"
    @update:open="value => !value && dismiss()"
  >
    <template #body>
      <UAlert v-if="error" class="mb-3" role="alert" color="error" variant="subtle" :description="error" />
      <template v-if="options.length">
        <label for="reviewer-select" class="block text-sm text-muted">Reviewers</label>
        <USelectMenu
          id="reviewer-select"
          v-model="chosen"
          :items="options"
          value-key="value"
          multiple
          aria-label="Reviewers"
          placeholder="Choose reviewers"
          class="mt-1 w-full"
          :disabled="pending"
        />
      </template>
      <div v-else id="reviewer-none" class="text-sm text-muted">
        <p>Forge could not find any known reviewers for this repository.</p>
        <p class="mt-2">
          Request a review directly on GitHub:
          <a
            :href="pullUrl"
            target="_blank"
            rel="noopener noreferrer"
            class="text-sky-200 underline-offset-4 hover:underline"
          >{{ pull ? `Open PR #${pull.number} on GitHub ↗` : 'Open the issue on GitHub ↗' }}</a>
        </p>
      </div>
    </template>
    <template #footer>
      <DialogFooter>
        <template #secondary>
          <UButton color="neutral" variant="ghost" :disabled="pending" @click="dismiss">Close</UButton>
        </template>
        <template v-if="options.length" #primary>
          <UButton color="primary" data-confirm :loading="pending" :disabled="!chosen.length || pending" :aria-busy="pending" @click="submit">{{ pending ? ACTION_PENDING_LABELS.assign_reviewer : ACTION_LABELS.assign_reviewer }}</UButton>
        </template>
      </DialogFooter>
    </template>
  </UModal>
</template>

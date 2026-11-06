<script setup lang="ts">
import { harnessLine, harnessState, type Harness, type SetupActivity } from '~/utils/onboarding'
import { accountAnchor, type AccountAlert } from '~/utils/accounts'

// One coding-account row. Per-harness state only: `checks` and `error` are
// page-level (contracts §3.7), so the error notice stays on the page and
// never moves in here.
// `alert`: this account's live health problem from the board poll, if any.
const props = defineProps<{ harness: Harness; activity?: SetupActivity | null; alert?: AccountAlert | null; browser?: boolean; operation?: string }>()
const emit = defineEmits<{ open: [harness: Harness] }>()

const line = computed(() => harnessLine(props.harness))
const state = computed(() => harnessState(props.harness, props.activity))
const BADGE = { sign_in_expired: 'Signed out', quota: 'Usage limit reached', unavailable: 'Not verified' } as const
</script>

<template>
  <div
    :id="accountAnchor(harness.id)"
    class="flex scroll-mt-40 items-center gap-4 border-b border-line px-5 py-4 last:border-b-0"
    :class="alert ? (alert.color === 'error' ? 'bg-error/10' : 'bg-warning/10') : ''"
    :data-harness="harness.id"
  >
    <div class="min-w-0 flex-1">
      <h3 class="flex flex-wrap items-center gap-2 text-[15px] font-medium break-words">
        {{ harness.name }}
        <UBadge v-if="alert" :color="alert.color" variant="solid" size="sm" data-account-status>{{ BADGE[alert.status] }}</UBadge>
      </h3>
      <p v-if="alert" class="mt-1 text-[13px] leading-relaxed" :class="alert.color === 'error' ? 'text-error' : 'text-warning'">{{ alert.title }} {{ alert.description }}</p>
      <small class="block text-[13px] leading-relaxed whitespace-pre-line text-muted">{{ line }}</small>
    </div>
    <UButton v-if="operation && ['queued', 'connecting', 'verifying'].includes(operation)" color="neutral" variant="outline" @click="emit('open', harness)">
      {{ operation === 'verifying' ? 'Verifying…' : 'Sign-in progress' }}
    </UButton>
    <UButton v-else-if="alert && state !== 'busy'" :color="alert.color" data-account-reconnect @click="emit('open', harness)">
      {{ browser ? alert.actionLabel : 'CLI instructions' }}
    </UButton>
    <span v-else-if="state === 'busy'" role="status" class="flex shrink-0 items-center gap-2 text-xs text-muted">
      <span class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
      {{ activity?.label }}
    </span>
    <span v-else-if="state === 'verified'" class="flex shrink-0 items-center gap-2 text-xs text-mint-200">
      <span aria-hidden="true">✓</span> Verified
    </span>
    <UButton v-else color="primary" @click="emit('open', harness)">
      {{ browser ? (state === 'verify' ? 'Verify connection' : 'Connect') : 'CLI instructions' }}
    </UButton>
  </div>
</template>

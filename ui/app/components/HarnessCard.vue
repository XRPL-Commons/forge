<script setup lang="ts">
import { harnessLine, harnessState, type Harness, type SetupActivity } from '~/utils/onboarding'

// One coding-account row. Per-harness state only: `checks` and `error` are
// page-level (contracts §3.7), so the error notice stays on the page and
// never moves in here.
const props = defineProps<{ harness: Harness; activity?: SetupActivity | null }>()
const emit = defineEmits<{ open: [harness: Harness] }>()

const line = computed(() => harnessLine(props.harness))
const state = computed(() => harnessState(props.harness, props.activity))
</script>

<template>
  <div class="flex items-center gap-4 border-b border-line px-5 py-4 last:border-b-0" :data-harness="harness.id">
    <div class="min-w-0 flex-1">
      <h3 class="text-[15px] font-medium break-words">{{ harness.name }}</h3>
      <small class="block text-[13px] leading-relaxed whitespace-pre-line text-muted">{{ line }}</small>
    </div>
    <span v-if="state === 'busy'" role="status" class="flex shrink-0 items-center gap-2 text-xs text-muted">
      <span class="size-3.5 rounded-full border border-line border-t-mint-200 motion-safe:animate-spin" aria-hidden="true" />
      {{ activity?.label }}
    </span>
    <span v-else-if="state === 'verified'" class="flex shrink-0 items-center gap-2 text-xs text-mint-200">
      <span aria-hidden="true">✓</span> Verified
    </span>
    <UButton v-else color="primary" @click="emit('open', harness)">
      {{ state === 'verify' ? 'Verify connection' : 'Connect' }}
    </UButton>
  </div>
</template>

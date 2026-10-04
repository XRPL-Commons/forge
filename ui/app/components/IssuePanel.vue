<script setup lang="ts">
import { issuePath } from '~/utils/issues'
import { panelState } from '~/utils/panel'
import { issueTab } from '~/utils/discussion'

// The board's detail panel: the same IssueDetail the /issues/<key> route
// renders, slid in over the right of the board. The board keeps polling
// behind it, and the panel is the issue's control centre — every decision,
// Stop and Reset live in the detail's action bar, not on the cards.
const props = defineProps<{ issueKey: string }>()
const open = defineModel<boolean>('open', { required: true })

// The panel is open before the issue's title is known, so its heading is the
// one thing the key already says.
const title = computed(() => {
  const number = props.issueKey.split(':')[1]
  return number ? `Issue #${number}` : props.issueKey
})
const route = useRoute()
const fullPage = computed(() => issuePath(props.issueKey, panelState(route.query).job, issueTab(route.query)))
</script>

<template>
  <USlideover
    v-model:open="open"
    side="right"
    :title="title"
    :ui="{ content: 'max-w-[560px]' }"
  >
    <template #actions>
      <UButton id="issue-full-page" :to="fullPage" size="xs" color="neutral" variant="ghost" trailing-icon="i-lucide-maximize-2">Open full page</UButton>
    </template>
    <template #body>
      <!-- Keyed by the issue, so opening another card starts from a clean
           panel instead of inheriting the previous issue's state. Its own
           Suspense boundary keeps the detail's first load off the board. -->
      <Suspense>
        <IssueDetail :key="issueKey" :issue-key="issueKey" fill="container" />
        <template #fallback>
          <p role="status" class="text-sm text-muted">Loading issue…</p>
        </template>
      </Suspense>
    </template>
  </USlideover>
</template>

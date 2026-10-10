<script setup lang="ts">
const props = withDefaults(defineProps<{ issueKey: string; fill?: 'container' | 'viewport' }>(), { fill: 'viewport' })
const controller = useIssueDetailController(props)
provideIssueDetail(controller)
const {
  detail, actions, problem, pending, busy, rendered, pipeline, look, badge, NOTE_TONE, conversation,
  decisions, primary, now, jobs, selectedJob, showOutput, internalJob, RESET, resetting,
  resetRequest, outputOpen, askReviewer, askReset, closeReset, confirmReset, ask, root, rootClass,
  filling, fillHeight, tab, TABS, chat, chatAttention, statusLabel, issueTab, conversationPrimary,
  output, facts, times, stageNote, statusReason, ready, sentence,
} = controller
await ready
</script>
<template>
  <div ref="root" :class="rootClass" :style="filling && fill === 'viewport' ? { height: fillHeight } : undefined">
    <UAlert v-if="problem" role="status" color="warning" variant="subtle" :description="problem" />
    <p v-else-if="!detail" role="status" class="text-sm text-muted">Loading issue…</p>

    <template v-if="detail">
      <!-- Header: where the issue lives, what it is and its status. It
           stays above both tabs, so the chat always says which issue it is. -->
      <p class="text-xs text-muted">{{ detail.repo }} · #{{ detail.number }}</p>
      <div class="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <h1 id="issue-title" class="min-w-0 text-lg font-semibold text-white wrap-anywhere sm:text-xl" :class="filling ? 'line-clamp-2' : ''">{{ detail.title }}</h1>
        <UBadge
          id="issue-status"
          :color="badge.color"
          :variant="badge.dim ? 'outline' : 'subtle'"
          :icon="badge.icon ?? undefined"
          :class="badge.dim ? 'text-muted' : ''"
          :data-tone="badge.dim ? 'calm' : undefined"
        >{{ statusLabel(detail) }}</UBadge>
      </div>

      <p v-if="statusReason" id="issue-status-reason" class="mt-1 text-sm text-muted">{{ statusReason }}</p>

      <UTabs
        :model-value="tab"
        :items="TABS"
        :unmount-on-hide="false"
        activation-mode="manual"
        variant="link"
        size="sm"
        aria-label="Issue views"
        class="mt-3"
        :class="filling ? 'min-h-0 flex-1' : ''"
        :ui="{ root: 'gap-0 items-stretch', list: 'p-0', trigger: 'min-h-11 sm:min-h-9' }"
        @update:model-value="value => tab = issueTab({ tab: String(value) })"
      >
        <template #trailing="{ item }">
          <span v-if="item.value === 'discussion' && chatAttention" id="discussion-tab-dot" class="inline-flex items-center">
            <span aria-hidden="true" class="size-2 rounded-full" :class="chat.replying ? 'bg-sky-300 motion-safe:animate-pulse' : 'bg-primary'" />
            <span class="sr-only">{{ chat.replying ? '(reply in progress)' : '(new reply)' }}</span>
          </span>
        </template>
        <template #details>
          <p v-if="stageNote || (pipeline?.attempts && !facts)" id="issue-attempt-line" class="mb-3 text-xs" :class="pipeline ? NOTE_TONE[pipeline.state] : ''">
            <span v-if="stageNote" id="issue-stage-note">{{ sentence(stageNote) }}</span>
            <template v-if="pipeline?.attempts && !facts">
              <template v-if="stageNote">{{ ' ' }}</template>
              <span id="issue-attempts" class="text-muted">{{ pipeline.attempts }}</span>
            </template>
          </p>

          <!-- The next step: the cube on its workbench says what is going on
               (while a worker runs, what it is doing right now); directly
               under it, who is on it and how to move the issue on. -->
          <IssueNextStep />

          <IssueStats v-if="look" :look="look" :times="times" :now="now" class="mt-4" />

          <!-- Everything else, quieter: where the work lives on GitHub, then
               the sections that have something in them, most relevant first. -->
          <IssueDetailSections />
        </template>
        <template #discussion>
          <IssueConversation
            ref="conversation"
            :key="detail.key"
            :active="tab === 'discussion'"
            :issue-key="detail.key"
            :actions="decisions"
            :primary="conversationPrimary"
            :disabled="busy"
            @act="(action, reason) => action === 'assign_reviewer' ? askReviewer() : ask(action, reason)"
            @attention="value => chat = value"
          />
        </template>
      </UTabs>

      <IssueDetailDialogs />
    </template>

    <!-- An internal run whose issue is not in the inventory keeps its one
         control and its output: the bar above belongs to the issue, this one
         to the run. -->
    <template v-else>
      <div v-if="internalJob" id="job-actions" class="mt-4 flex flex-wrap items-center gap-2">
        <UButton size="sm" color="error" variant="ghost" :disabled="busy" @click="askReset">Reset runner</UButton>
      </div>
      <IssueWorkerOutput
        v-if="showOutput"
        v-model:open="outputOpen"
        :jobs="jobs"
        :selected-job="selectedJob"
        :output="output.data.value"
        :error="output.error.value"
        :updated-at="output.updatedAt.value"
        class="mt-4"
      />
    </template>

    <!-- Reset targets the run, not the issue, so it lives outside the block
         the issue's own decisions are rendered in. -->
    <ConfirmDialog
      v-if="resetting"
      :open="resetting"
      :title="RESET.title"
      :text="RESET.text"
      :confirm-label="RESET.confirmLabel"
      :error="resetRequest.error.value"
      :pending="resetRequest.pending.value"
      pending-label="Resetting…"
      @confirm="confirmReset"
      @close="closeReset"
    />
  </div>
</template>

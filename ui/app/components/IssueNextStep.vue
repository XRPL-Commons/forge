<script setup lang="ts">
const {
  verificationRequest, verificationStatement, poll, detail, actions, pending, busy, decision,
  message, summary, look, blocking, plainOpen, plainLong, holds, issueUrl, canAssign, decisions,
  setupNeeded, primary, secondary, approvalOpen, sentToGitHub, approval, now, reviewRequested,
  approvals, checked, checkingSince, checkNote, checkRequest, deployment, internalJob, siblingLink,
  askReviewer, checkNow, askReset, ask, confirmVerification, tab, ACTION_LABELS, attentionNote,
} = useIssueDetailContext()
</script>
<template>
  <template v-if="detail">
          <section id="issue-next" class="flex flex-col gap-3" aria-labelledby="issue-next-title">
            <h2 id="issue-next-title" class="sr-only">Next step</h2>
            <IssueWorkbench v-if="look" :look="look">
              <p v-if="summary?.headline" id="issue-headline" class="text-[15px] leading-snug font-semibold text-balance text-white">{{ summary.headline }}</p>
              <p
                v-if="summary?.plain"
                id="issue-plain"
                class="text-sm text-[#c5cad1] wrap-anywhere"
                :class="[summary?.headline ? 'mt-1.5' : '', plainLong && !plainOpen ? 'line-clamp-3' : '']"
              >{{ summary.plain }}</p>
              <UButton
                v-if="plainLong"
                size="xs"
                color="neutral"
                variant="link"
                class="px-0"
                :aria-expanded="plainOpen"
                @click="plainOpen = !plainOpen"
              >{{ plainOpen ? 'less' : 'more' }}</UButton>
              <ul v-if="holds.length" id="issue-held-by" class="mt-1.5 text-xs text-muted">
                <li v-for="line in holds" :key="line">{{ line }}</li>
              </ul>
              <p v-if="!summary && !holds.length" class="text-sm text-muted">Forge has no summary for this issue yet.</p>
            </IssueWorkbench>

            <div id="issue-action-block" class="flex flex-col gap-2.5">
              <section v-if="detail.decision_request" id="issue-decision" class="space-y-2 text-sm" aria-labelledby="decision-question">
                <h3 id="decision-question" class="font-semibold">{{ detail.decision_request.question }}</h3>
                <ul class="list-disc space-y-1 pl-5">
                  <li v-for="option in detail.decision_request.options" :key="option">{{ option }}</li>
                </ul>
                <p><strong>Recommendation:</strong> {{ detail.decision_request.recommendation }}</p>
                <p class="text-muted">{{ detail.decision_request.evidence }}</p>
                <p class="text-muted">Discuss the options below, then choose Retry attempt and record your answer.</p>
              </section>
              <p v-if="approval && (sentToGitHub || !summary?.headline)" role="status" class="text-sm text-amber-200">Waiting for your approval on GitHub</p>
              <ul
                v-if="approvals.length"
                id="issue-approvals"
                class="flex flex-col gap-1 rounded-lg border border-line bg-panel px-2.5 py-2 text-sm"
                aria-label="Reviews Forge found on GitHub"
              >
                <li v-for="line in approvals" :key="line.login" class="flex items-start gap-1.5" :class="line.counted ? 'text-mint-200' : 'text-muted'">
                  <UIcon :name="line.icon" class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <span>{{ line.text }}</span>
                </li>
              </ul>
              <p v-else-if="approval" id="issue-approvals-empty" class="text-sm text-muted">No approval found on the pull request yet.</p>
              <p v-if="detail.merge_blocker" class="text-sm text-amber-200">Check the pull request checks and repository rules on GitHub. If branch rules block the controller, configure its pull-request bypass in Repository setup. Forge will retry after the blocker is resolved.</p>
              <p v-if="reviewRequested" id="review-requested" class="text-sm text-muted">{{ reviewRequested }}</p>
              <p v-if="deployment" id="issue-deployment" class="text-sm text-muted">
                Deployment · {{ deployment.text }}
                <a
                  v-if="deployment.runUrl"
                  :href="deployment.runUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="ml-1 text-sky-200 underline-offset-4 hover:underline"
                >Deploy run ↗</a>
              </p>

              <!-- What this issue waits for: one chip per dependency. -->
              <div v-if="detail.dependencies.length" id="issue-dependencies" class="flex flex-wrap items-center gap-1.5 text-xs text-muted">
                <span>Depends on</span>
                <NuxtLink
                  v-for="number in detail.dependencies"
                  :key="number"
                  :to="siblingLink(number)"
                  class="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel py-0.5 pr-2.5 pl-1.5 text-white hover:border-muted"
                >
                  <span aria-hidden="true" class="size-3 rounded-[3px] border-[1.5px] border-[#0a0c0f] bg-[#59606b]" />
                  <span class="font-mono">#{{ number }}</span>
                </NuxtLink>
                <a :href="issueUrl" target="_blank" rel="noopener noreferrer" class="ml-1 underline underline-offset-4">Edit dependencies on GitHub ↗</a>
              </div>

              <p v-if="message" id="detail-message" role="status" class="text-sm text-amber-200">{{ message }}</p>

              <section v-if="detail.verification_request" id="issue-verification" class="space-y-2 rounded-lg border border-line bg-panel p-4 text-sm">
                <h3 class="font-medium">Verification needed</h3>
                <p class="text-muted">{{ detail.verification_request.why }}</p>
                <pre class="whitespace-pre-wrap break-words font-sans">{{ detail.verification_request.what }}</pre>
                <p v-if="detail.verification_request.ci?.length" class="text-muted">Waiting for configured CI checks: {{ detail.verification_request.ci.map(check => check.name).join(', ') }}. Forge checks them automatically.</p>
                <template v-else-if="detail.verification_request.can_confirm">
                  <p class="text-muted">Run these steps on the current pull request revision. Record what you checked and confirm only after they pass.</p>
                  <UTextarea v-model="verificationStatement" aria-label="Verification evidence" placeholder="What I checked and the result" :maxlength="2000" :disabled="busy" class="w-full" />
                  <UButton :disabled="busy || !verificationStatement.trim()" :loading="verificationRequest.pending.value" @click="confirmVerification">Confirm verification passed</UButton>
                  <p v-if="verificationRequest.error.value" role="alert" class="text-error">{{ verificationRequest.error.value }}</p>
                </template>
                <p v-else class="text-muted">Run these steps on the current PR revision, then approve on GitHub if they pass. If they fail, comment on the PR and retry with the findings.</p>
              </section>

              <!-- One primary way forward (or a calm line when there is none),
                   smaller secondary decisions, then stopping the attempt. -->
              <!-- Dropped work asks nothing: the only way back is on GitHub. -->
              <div v-if="detail.status === 'dropped'" id="issue-actions" class="flex flex-wrap items-center gap-2">
                <span id="issue-nothing-needed" data-dropped class="mr-1 inline-flex min-h-8 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm text-muted">
                  <UIcon name="i-lucide-archive" class="size-4 shrink-0" aria-hidden="true" />
                  <span>Nothing needed from you.</span>
                  <a
                    id="issue-reopen"
                    :href="issueUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="text-sky-200 underline-offset-4 hover:underline"
                  >Reopen it on GitHub to bring it back ↗</a>
                </span>
              </div>
              <div v-else id="issue-actions" class="flex flex-wrap items-center gap-2">
                <UButton v-if="primary === 'approve'" color="primary" :disabled="busy || poll.stale.value" @click="approvalOpen = true">Approve…</UButton>
                <UButton v-else-if="primary === 'assign_reviewer'" color="primary" :disabled="busy" @click="askReviewer">{{ ACTION_LABELS.assign_reviewer }}</UButton>
                <UButton v-else-if="primary === 'setup'" :to="detail.setup_url" color="primary">Repository setup →</UButton>
                <UButton v-else-if="primary" color="primary" :disabled="busy" @click="ask(primary)">{{ ACTION_LABELS[primary] }}</UButton>
                <span v-else-if="detail.status === 'blocked'" id="issue-nothing-needed" data-blocked class="mr-1 inline-flex min-h-8 flex-wrap items-center gap-x-1 gap-y-0.5 text-sm text-muted">
                  <UIcon name="i-lucide-link-2" class="mr-1 size-4 shrink-0" aria-hidden="true" />
                  <span>Nothing needed from you.</span>
                  <template v-if="blocking.length">
                    <span>Starts by itself when</span>
                    <template v-for="(number, index) in blocking" :key="number">
                      <span class="whitespace-nowrap"><NuxtLink
                        :to="siblingLink(number)"
                        :data-blocking="number"
                        class="font-mono text-white underline-offset-4 hover:underline"
                      >#{{ number }}</NuxtLink>{{ index < blocking.length - 2 ? ',' : '' }}</span>
                      <span v-if="index === blocking.length - 2">and</span>
                    </template>
                    <span>{{ blocking.length === 1 ? 'closes.' : 'close.' }}</span>
                  </template>
                  <span v-else>Starts by itself when the issues it waits on close.</span>
                </span>
                <span v-else-if="detail.status === 'attention'" id="issue-needs-person" class="mr-1 inline-flex min-h-8 items-center gap-2 text-sm text-amber-200">
                  <span aria-hidden="true" class="size-2 shrink-0 rounded-full bg-amber-200" />
                  {{ attentionNote(detail) }}
                </span>
                <span v-else id="issue-nothing-needed" class="mr-1 inline-flex min-h-8 items-center gap-2 text-sm text-muted">
                  <span aria-hidden="true" class="size-2 rounded-full bg-mint-400" />
                  {{ look?.mood === 'happy' ? 'Done. Nothing left to do.' : 'Nothing needed from you right now.' }}
                </span>
                <UButton
                  v-for="action in secondary"
                  :key="action"
                  color="neutral"
                  variant="outline"
                  size="sm"
                  :disabled="busy"
                  @click="ask(action)"
                >{{ ACTION_LABELS[action] }}</UButton>
                <UButton
                  v-if="canAssign && primary !== 'assign_reviewer'"
                  size="sm"
                  color="neutral"
                  variant="outline"
                  :disabled="busy"
                  @click="askReviewer"
                >{{ ACTION_LABELS.assign_reviewer }}</UButton>
                <UButton
                  size="sm"
                  color="neutral"
                  variant="outline"
                  @click="tab = 'discussion'"
                >Discuss blocker</UButton>
                <UButton v-if="setupNeeded && primary !== 'setup'" :to="detail.setup_url" size="sm" color="neutral" variant="outline">Repository setup →</UButton>
                <span v-if="(detail.active && detail.status === 'working') || internalJob" class="ml-auto flex flex-wrap items-center gap-1">
                  <UButton
                    v-if="detail.active && detail.status === 'working'"
                    size="sm"
                    color="error"
                    variant="ghost"
                    :disabled="busy"
                    @click="ask('stop')"
                  >{{ ACTION_LABELS.stop }}</UButton>
                  <UButton
                    v-if="internalJob"
                    size="sm"
                    color="error"
                    variant="ghost"
                    :disabled="busy"
                    @click="askReset"
                  >Reset runner</UButton>
                </span>
              </div>

              <!-- The quiet way out, and how fresh this is. -->
              <div id="issue-meta" class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-dashed border-line pt-2 text-xs text-muted">
                <UButton
                  v-if="detail.closable"
                  id="issue-close"
                  size="xs"
                  color="error"
                  variant="ghost"
                  icon="i-lucide-circle-x"
                  class="-ml-2"
                  :disabled="busy"
                  @click="ask('close')"
                >Close issue…</UButton>
                <span v-else />
                <div id="issue-checked" class="flex flex-wrap items-center gap-1">
                  <span v-if="checkingSince" role="status">Checking GitHub…</span>
                  <span v-else-if="checked">{{ checked }}</span>
                  <span v-if="checked || checkingSince" aria-hidden="true">·</span>
                  <UButton size="xs" color="neutral" variant="ghost" icon="i-lucide-refresh-cw" class="-mx-1" :loading="checkRequest.pending.value || !!checkingSince" :disabled="checkRequest.pending.value || !!checkingSince" @click="checkNow">Check now</UButton>
                  <span v-if="checkNote" role="status" class="basis-full">{{ checkNote }}</span>
                </div>
              </div>
            </div>
          </section>


  </template>
</template>

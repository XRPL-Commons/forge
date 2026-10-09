<script setup lang="ts">
const {
  environmentOpen, environmentRequest, detail, busy, rendered, html, descriptionOpen,
  environmentSectionOpen, hasFindings, issueUrl, jobs, selectedJob, outputOpen, activityOpen,
  pullUrl, localTime, isoTime, saveEnvironment, eventLabel, pullLabel, output, order,
} = useIssueDetailContext()
</script>
<template>
  <template v-if="detail">
          <p id="issue-links" class="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <a :href="issueUrl" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">GitHub issue ↗</a>
            <a
              v-for="pull in detail.pull_requests"
              :key="pull.number"
              :href="pullUrl(pull.number)"
              :title="pull.title"
              target="_blank"
              rel="noopener noreferrer"
              class="text-sky-200 underline-offset-4 hover:underline"
            >{{ pullLabel(pull) }} ↗</a>
          </p>

          <LazyIssueDelivery v-if="detail.delivery_id" :issue-key="detail.key" />

          <div id="issue-sections" class="mt-3 flex flex-col border-b border-line">
            <IssueReviewFindings v-if="hasFindings" :detail="detail!" :style="{ order: order('findings') }" />

            <IssueWorkerOutput
              v-if="selectedJob || jobs.length"
              v-model:open="outputOpen"
              :jobs="jobs"
              :selected-job="selectedJob"
              :output="output.data.value"
              :error="output.error.value"
              :updated-at="output.updatedAt.value"
              :style="{ order: order('output') }"
            />

            <!-- Collapsed by default but always rendered, so the server-rendered
                 description is in the page from the first paint. -->
            <IssueSection v-if="detail.body || detail.body_truncated" id="issue-description" v-model:open="descriptionOpen" title="Description" :unmount-on-hide="false" :style="{ order: order('description') }">
              <div class="rounded-xl border border-line bg-panel p-4">
                <!-- Loaded lazily on navigation: say nothing until it arrives, not "no description". -->
                <MarkdownBody v-if="html != null" :html="html" />
                <p v-if="detail.body_truncated" class="mt-2 text-xs text-amber-200">Description shortened. Open on GitHub to read the full issue.</p>
              </div>
            </IssueSection>

            <IssueSection v-if="detail.environment" id="issue-environment-section" v-model:open="environmentSectionOpen" title="Worker environment" :style="{ order: order('environment') }">
              <IssueEnvironmentCard
                v-model:open="environmentOpen"
                :environment="detail.environment"
                :busy="busy"
                :error="environmentRequest.error.value ?? ''"
                @save="saveEnvironment"
              />
            </IssueSection>

            <IssueSection v-if="detail.history.length" id="issue-activity" v-model:open="activityOpen" title="Recent activity" :style="{ order: order('activity') }">
              <ul class="flex flex-col gap-2 text-xs">
                <li v-for="(event, index) in detail.history" :key="`${event.at}-${index}`">
                  <time :datetime="isoTime(event.at)" class="text-muted">{{ localTime(event.at) }}</time>
                  <span class="ml-2 text-white">{{ eventLabel(event.kind) }}</span>
                  <p v-if="event.reason" class="text-muted wrap-anywhere">{{ event.reason }}</p>
                </li>
              </ul>
            </IssueSection>
          </div>
  </template>
</template>

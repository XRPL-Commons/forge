<script setup lang="ts">
import { branchText, statusPillText, repoDescription, type RepositoryRow, type ProtectionReport } from '~/utils/repositories'
import { TONE_BADGE } from '~/utils/tones'
const props = defineProps<{ row: RepositoryRow; live: boolean; report: ProtectionReport | null; displayReady: boolean; hint: string; idPrefix?: string }>()
const prefix = computed(() => props.idPrefix || '')
const additionalRules = computed(() => [...new Set((props.report?.additional_rules ?? []).map(rule => rule.type.replaceAll('_', ' ')))])
</script>
<template>
  <section :id="prefix + 'repository-summary'" class="mt-4 rounded-xl border border-line bg-panel p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="break-words text-lg font-semibold text-white">{{ displayReady ? `Repository checks ready for ${row.name}` : `Check repository setup for ${row.name}` }}</h2>
        <p class="mt-0.5 text-xs text-muted">{{ branchText(report) }}</p>
      </div>
      <UBadge :id="prefix + 'status'" tabindex="-1" :color="TONE_BADGE[displayReady ? 'ok' : 'warn']" variant="subtle">{{ statusPillText(row, live) }}</UBadge>
    </div>
    <p :id="prefix + 'description'" class="mt-2 text-sm text-muted">{{ repoDescription(row, live) }}</p>
    <p v-if="hint" :id="prefix + 'hint'" class="mt-3 text-xs text-amber-200">{{ hint }}</p>
    <div class="mt-4 border-t border-line pt-4 text-sm">
      <h3 class="font-semibold text-white">Forge merge policy</h3>
      <p class="mt-2 text-muted">The supervisor merges Forge work after CI, independent Review and Audit pass.</p>
      <p v-if="row.merge_policy === 'human'" class="mt-2 text-muted">A counted human approval is also required before merge.</p>
      <p class="mt-2 text-muted">{{ row.merge_check_scope === 'forge' ? 'Forge Review and Audit apply to Forge-created pull requests.' : 'Forge Review and Audit apply to all pull requests. Human pull requests stay under their author’s control.' }}</p>
    </div>
  </section>
  <section :id="prefix + 'github-rules'" class="mt-4 rounded-xl border border-line bg-bg p-4 text-sm" aria-label="Existing GitHub rules">
    <h3 class="font-semibold text-white">Existing GitHub rules</h3>
    <p class="mt-2 text-muted">Repository owners manage these rules. Forge observes them and reports any checks or reviews blocking a pull request.</p>
    <template v-if="report?.available">
      <ul v-if="report.required_approvals || report.code_owner_reviews || additionalRules.length" class="mt-3 list-disc space-y-1 pl-5 text-muted">
        <li v-if="report.required_approvals">{{ report.required_approvals }} approving {{ report.required_approvals === 1 ? 'review is' : 'reviews are' }} required by GitHub.</li>
        <li v-if="report.code_owner_reviews">Code owner approval is required.</li>
        <li v-for="rule in additionalRules" :key="rule">{{ rule }}</li>
      </ul>
      <ul v-if="report.sources?.length" class="mt-3 space-y-1">
        <li v-for="source in report.sources" :key="source.url"><a :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline">{{ source.name }} ↗</a></li>
      </ul>
    </template>
    <p v-else class="mt-2 text-muted">Current GitHub rules have not been observed yet.</p>
    <a v-if="row.rules_url" :href="row.rules_url" target="_blank" rel="noopener noreferrer" class="mt-3 inline-block text-sky-200 underline">View GitHub rules ↗</a>
  </section>
</template>

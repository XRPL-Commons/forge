<script setup lang="ts">
import { branchText, statusPillText, repoDescription, rulesetFilename, scopeText, requirements, type RepositoryRow, type ProtectionReport, type ProtectionSource } from '~/utils/repositories'
import { TONE_BADGE } from '~/utils/tones'
const props = defineProps<{ row: RepositoryRow; live: boolean; report: ProtectionReport | null; displayReady: boolean; shown: string | undefined; failed: ReturnType<typeof requirements>; hint: string; prepared: boolean; editUrl: string; downloadHref?: string; downloaded: boolean; instructions?: boolean; idPrefix?: string; controllerSlug?: string | null }>()
defineEmits<{ download: [] }>()
const detailsOpen = ref(false)
const whyOpen = ref(false)
const bypass = computed(() => props.report?.bypass)
const pending = computed(() => bypass.value?.status === 'missing' ? bypass.value.sources ?? [] : [])
const seen = shallowRef<ProtectionSource[]>([])
const failedIds = ref<string[]>([])
watch(() => props.row.id, () => { seen.value = []; failedIds.value = [] })
watch([pending, () => props.failed], ([sources, failed]) => {
  const remembered = new Map(seen.value.map(source => [source.url, source]))
  for (const source of sources) remembered.set(source.url, source)
  seen.value = [...remembered.values()]
  failedIds.value = [...new Set([...failedIds.value, ...failed.map(requirement => requirement.id)])]
}, { immediate: true })
const requirementFixes = computed(() => requirements(props.report).filter(requirement => failedIds.value.includes(requirement.id)))
const fixed = (source: ProtectionSource) => bypass.value?.status === 'ready' || (bypass.value?.status === 'missing' && !pending.value.some(current => current.url === source.url))
const prefix = computed(() => props.idPrefix || '')
</script>
<template>
  <section :id="prefix + 'repository-summary'" class="mt-4 rounded-xl border border-line bg-panel p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="break-words text-lg font-semibold text-white">{{ displayReady ? `Review rules ready for ${row.name}` : `Let Forge merge approved pull requests in ${row.name}` }}</h2>
        <p class="mt-0.5 text-xs text-muted">{{ branchText(report) }}</p>
      </div>
      <UBadge :id="prefix + 'status'" tabindex="-1" :color="TONE_BADGE[displayReady ? 'ok' : 'warn']" variant="subtle">{{ statusPillText(row, live) }}</UBadge>
    </div>
    <p v-if="displayReady" :id="prefix + 'description'" class="mt-2 text-sm text-muted">{{ repoDescription(row, live) }}</p>
    <p v-if="hint" :id="prefix + 'hint'" class="mt-3 text-xs text-amber-200">{{ hint }}</p>
    <div v-if="seen.length" :id="pending.length ? prefix + 'bypass-missing' : undefined" class="mt-3 space-y-3">
      <div v-for="(source, index) in seen" :id="prefix + (source.kind === 'classic' ? 'bypass-classic' : 'bypass-rulesets') + (index ? `-${index}` : '')" :key="source.url">
        <RepositoryRuleFixCard :source="source" :repo="row.name" :branch="report?.branch || 'main'" :app-id="bypass?.controller_app_id" :app-slug="controllerSlug" :prefix="prefix + (index ? `rule-${index}-` : '')" :fixed="fixed(source)" />
      </div>
    </div>
    <div v-if="requirementFixes.length" :id="prefix + 'requirements'" class="mt-3 space-y-3">
      <section v-for="requirement in requirementFixes" :key="requirement.id" data-requirement-fix class="rounded-lg border border-line bg-bg p-3">
        <RepositoryRequirementList :items="[requirement]" />
        <UBadge v-if="requirement.passed" color="success" variant="subtle" class="mt-2">Fixed</UBadge>
        <ol v-else class="mt-2 list-decimal space-y-1 pl-5 text-xs text-muted">
          <li>Open <a :href="editUrl || row.rules_url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline">review rule settings ↗</a>.</li>
          <li>{{ requirement.id === 'reviews' ? 'Require pull requests with at least two approving reviews.' : 'Select Dismiss stale pull request approvals when new commits are pushed.' }}</li>
          <li>Save, then check again here.</li>
        </ol>
      </section>
    </div>
    <UCollapsible v-if="!displayReady" v-model:open="whyOpen" class="mt-3">
      <UButton color="neutral" variant="link" size="xs" class="px-0">Why?</UButton>
      <template #content><p class="text-xs text-muted">GitHub applies classic protection and rulesets together. Each approval rule needs the controller App’s bypass so Forge can merge work after its review and audit.</p></template>
    </UCollapsible>
  </section>
  <section v-if="instructions && !displayReady && prepared && !seen.length && !editUrl" :id="prefix + 'steps'" class="mt-4 rounded-xl border border-line bg-bg p-4">
    <h3 class="text-sm font-semibold">Create the review rules</h3>
    <ol class="mt-2 list-decimal space-y-2 pl-5 text-xs text-muted">
      <li :id="prefix + 'download-step'"><UButton :id="prefix + 'download'" :to="downloadHref" :download="rulesetFilename(row)" :disabled="!downloadHref" external size="xs" variant="outline" @click="$emit('download')">{{ downloaded ? 'Download again' : 'Download ruleset' }}</UButton></li>
      <li><a :id="prefix + 'github'" :href="row.rules_url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline">Open GitHub settings ↗</a>, choose New ruleset → Import a ruleset, then save.</li>
      <li>Check again here.</li>
    </ol>
    <p :id="prefix + 'scope'" class="mt-2 text-xs text-muted">{{ scopeText(report) }}</p>
  </section>
  <UCollapsible v-model:open="detailsOpen" class="mt-4">
    <UButton color="neutral" variant="link" class="px-0 text-xs">Setup details</UButton>
    <template #content>
      <div class="pt-3 text-xs text-muted">
        <RepositoryRequirementList :items="requirements(report)" />
        <p v-if="bypass?.status === 'ready'" :id="prefix + 'bypass'" class="mt-2">✓ The Forge controller can merge approved work past these rules.</p>
        <p v-else-if="bypass?.status === 'operator_review'" :id="prefix + 'bypass'" class="mt-2">Bypass permissions need your review. Open the source rules on GitHub to check their bypass lists.</p>
        <ul v-if="report?.sources?.length" class="mt-2 space-y-1"><li v-for="source in report.sources" :key="source.url"><a :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline">{{ source.name }} ↗</a></li></ul>
      </div>
    </template>
  </UCollapsible>
</template>

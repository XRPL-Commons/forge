<script setup lang="ts">
import { branchText, statusPillText, repoDescription, rulesetFilename, scopeText, requirements, otherRulesText, type RepositoryRow, type ProtectionReport } from '~/utils/repositories'
import { TONE_BADGE } from '~/utils/tones'
defineProps<{ row: RepositoryRow; live: boolean; report: ProtectionReport | null; displayReady: boolean; shown: string | undefined; failed: ReturnType<typeof requirements>; hint: string; prepared: boolean; editUrl: string; downloadHref?: string; downloadAriaDisabled: 'true' | 'false'; downloaded: boolean; instructions?: boolean; idPrefix?: string }>()
defineEmits<{ download: [] }>()
const detailsOpen = ref(false)
</script>
<template>
  <section :id="(idPrefix || '') + 'repository-summary'" class="mt-4 rounded-xl border border-line bg-panel p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="break-all text-lg font-semibold text-white">{{ row.name }}</h2>
        <p class="mt-0.5 text-xs text-muted">{{ branchText(report) }}</p>
      </div>
      <div class="flex items-center gap-3">
        <UBadge :id="(idPrefix || '') + 'status'" tabindex="-1" :color="TONE_BADGE[displayReady ? 'ok' : 'warn']" variant="subtle">{{ statusPillText(row, live) }}</UBadge>
      </div>
    </div>
    <p :id="(idPrefix || '') + 'description'" class="mt-2 text-sm text-muted">{{ repoDescription(row, live) }}</p>

    <ul v-if="shown === 'needs_setup' && failed.length" :id="(idPrefix || '') + 'requirements'" class="mt-3 flex flex-col gap-2">
      <li v-for="requirement in failed" :key="requirement.id" class="flex gap-2 text-xs">
        <span aria-hidden="true" class="text-amber-200">○</span>
        <div>
          <strong class="text-sm font-medium text-white">{{ requirement.label }}</strong>
          <p class="text-muted">{{ requirement.detail }}</p>
        </div>
      </li>
    </ul>

    <p v-if="hint" :id="(idPrefix || '') + 'hint'" class="mt-3 text-xs text-amber-200">{{ hint }}</p>
  </section>

  <!-- Applying the review rules on GitHub. -->
  <section v-if="instructions && !displayReady && prepared" :id="(idPrefix || '') + 'steps'" class="mt-4 rounded-xl border border-line bg-bg p-4">
    <ol class="flex flex-col gap-4">
      <li v-if="!editUrl" :id="(idPrefix || '') + 'download-step'">
        <h3 class="text-sm font-semibold text-white">Download the review rules</h3>
        <p class="mt-0.5 text-xs text-muted">A ready-to-import file. No editing or administrator token needed.</p>
        <a
          :id="(idPrefix || '') + 'download'"
          class="mt-2 inline-block rounded-full border border-mint-200 px-3 py-1 text-xs text-mint-200"
          :href="downloadHref"
          :download="rulesetFilename(row)"
          :aria-disabled="downloadAriaDisabled"
          @click="$emit('download')"
        >{{ downloaded ? 'Download again' : 'Download ruleset' }}</a>
      </li>
      <li>
        <h3 :id="(idPrefix || '') + 'import-title'" class="text-sm font-semibold text-white">{{ editUrl ? 'Update the existing ruleset' : 'Import on GitHub' }}</h3>
        <p :id="(idPrefix || '') + 'import-description'" class="mt-0.5 text-xs text-muted">
          {{ editUrl
            ? 'A Forge review ruleset already exists. Update its review settings using the requirements above.'
            : 'On GitHub, choose New ruleset → Import a ruleset. Select the file, review the settings and save.' }}
        </p>
        <a
          :id="(idPrefix || '') + 'github'"
          class="mt-2 inline-block rounded-full border border-line px-3 py-1 text-xs text-sky-200"
          :href="editUrl || row.rules_url"
          target="_blank"
          rel="noopener noreferrer"
        >{{ editUrl ? 'Edit ruleset on GitHub ↗' : 'Open GitHub settings ↗' }}</a>
        <p :id="(idPrefix || '') + 'scope'" class="mt-2 text-xs text-muted">{{ scopeText(report) }}</p>
      </li>
      <li>
        <h3 class="text-sm font-semibold text-white">Return here to check</h3>
        <p class="mt-0.5 text-xs text-muted">After saving on GitHub, check that the review requirements are in place.</p>
      </li>
    </ol>
  </section>

  <UCollapsible v-model:open="detailsOpen" class="mt-4">
    <UButton color="neutral" variant="link" class="px-0 text-xs">Setup details</UButton>
    <template #content>
      <div class="pt-3 text-xs text-muted">
        <ul class="flex flex-col gap-2">
          <li v-for="requirement in requirements(report)" :key="requirement.id" class="flex gap-2">
            <span aria-hidden="true" :class="requirement.passed ? 'text-mint-200' : 'text-amber-200'">{{ requirement.passed ? '✓' : '○' }}</span>
            <div>
              <strong class="text-sm font-medium text-white">{{ requirement.label }}</strong>
              <p>{{ requirement.detail }}</p>
            </div>
          </li>
        </ul>
        <p class="mt-3">Forge reads review requirements using its existing GitHub access. It cannot change your rules.</p>
        <p class="mt-2"><strong class="text-white">Bypass permissions need your review.</strong> GitHub may hide who can bypass a ruleset. Check the bypass list on GitHub; a successful check here does not certify that list.</p>
        <p class="mt-2">{{ otherRulesText(report) }}</p>
        <ul v-if="report?.sources?.length" class="mt-2 flex flex-col gap-1">
          <li v-for="source in report.sources" :key="source.url">
            <a :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">{{ source.name }}{{ source.kind === 'ruleset' ? ' · ruleset' : '' }} ↗</a>
          </li>
        </ul>
        <NuxtLink to="/setup-help#github-repository-protection" class="mt-3 inline-block text-sky-200 underline-offset-4 hover:underline">Read the setup guide →</NuxtLink>
      </div>
    </template>
  </UCollapsible>
</template>

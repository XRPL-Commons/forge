<script setup lang="ts">
import { branchText, statusPillText, repoDescription, rulesetFilename, scopeText, requirements, otherRulesText, bypassFix, bypassSourceLabel, classicBypassCommand, type RepositoryRow, type ProtectionReport, type ProtectionSource } from '~/utils/repositories'
import { TONE_BADGE } from '~/utils/tones'
const props = defineProps<{ row: RepositoryRow; live: boolean; report: ProtectionReport | null; displayReady: boolean; shown: string | undefined; failed: ReturnType<typeof requirements>; hint: string; prepared: boolean; editUrl: string; downloadHref?: string; downloaded: boolean; instructions?: boolean; idPrefix?: string; controllerSlug?: string | null }>()
defineEmits<{ download: [] }>()
const detailsOpen = ref(false)
const bypass = computed(() => props.report?.bypass ?? null)
const fix = computed(() => bypassFix(props.report))
const branch = computed(() => props.report?.branch || 'main')
const appName = computed(() => props.controllerSlug || (bypass.value?.controller_app_id ? `ID ${bypass.value.controller_app_id}` : 'Forge controller'))
const classicSettings = computed(() => `https://github.com/${props.row.name}/settings/branches`)
const classicSource = computed<ProtectionSource | undefined>(() => fix.value?.classic[0])
const command = computed(() => props.controllerSlug ? classicBypassCommand(props.row.name, branch.value, props.controllerSlug, classicSource.value?.bypass_allowances) : '')
const keptAllowances = computed(() => {
  const current = classicSource.value?.bypass_allowances
  return current ? [...current.users, ...current.teams, ...current.apps] : []
})

// The UI is often served over plain HTTP, where `navigator.clipboard` is
// missing: selecting the command is then the normal path.
const COPY = 'Copy command'
const copyLabel = ref(COPY)
const commandBlock = useTemplateRef<HTMLElement>('commandBlock')
watch(command, () => { copyLabel.value = COPY })
async function copy() {
  try {
    await navigator.clipboard.writeText(command.value)
    copyLabel.value = 'Copied'
  } catch {
    copyLabel.value = 'Select and copy the command above'
    const element = commandBlock.value
    if (!element) return
    const range = document.createRange()
    range.selectNodeContents(element)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
  }
}
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

    <UAlert v-if="fix" :id="(idPrefix || '') + 'bypass-missing'" class="mt-3" role="status" color="warning" variant="subtle" title="Forge cannot merge approved work">
      <template #description>
        <ul class="flex flex-col gap-1">
          <li v-for="source in bypass?.sources ?? []" :key="source.url">
            <a :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">{{ bypassSourceLabel(source, report?.branch) }} ↗</a>
            has no bypass for the Forge controller.
          </li>
          <li v-if="!bypass?.sources?.length">These rules have no bypass for the Forge controller.</li>
        </ul>
        <p class="mt-1">
          The controller App<template v-if="bypass?.controller_app_id"> (ID {{ bypass.controller_app_id }})</template> must be allowed to bypass required pull request reviews on each of them. Classic branch protection and rulesets apply together, so each one needs it.
          <template v-if="fix.rulesets.length">Importing another ruleset does not repair an existing one.</template>
        </p>
      </template>
    </UAlert>

    <div v-if="fix" :id="(idPrefix || '') + 'bypass-steps'" class="mt-3 flex flex-col gap-3">
      <div v-if="fix.classic.length" :id="(idPrefix || '') + 'bypass-classic'" class="rounded-lg border border-line bg-bg p-3">
        <h3 class="text-sm font-semibold text-white">Let Forge bypass the branch protection rule</h3>
        <ol class="mt-1 list-decimal pl-5 text-xs text-muted">
          <li>Open <a :href="classicSettings" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">Branch protection settings ↗</a> and edit the <strong class="text-white">{{ branch }}</strong> rule.</li>
          <li>Under “Require a pull request before merging”, tick “Allow specified actors to bypass required pull requests”.</li>
          <li>Add the App <strong class="text-white">{{ appName }}</strong>, then save.</li>
        </ol>
        <template v-if="command">
          <p class="mt-3 text-xs text-muted">Or run this with the GitHub CLI as a repository administrator:</p>
          <pre ref="commandBlock" :id="(idPrefix || '') + 'bypass-command'" class="mt-1 rounded-lg border border-line bg-panel p-3 text-[12px] whitespace-pre-wrap break-all">{{ command }}</pre>
          <UButton :id="(idPrefix || '') + 'bypass-copy'" size="xs" variant="outline" class="mt-2" @click="copy">{{ copyLabel }}</UButton>
          <p :id="(idPrefix || '') + 'bypass-command-note'" class="mt-2 text-xs text-muted">
            <template v-if="classicSource?.bypass_allowances">This replaces the rule’s bypass list. It keeps the actors already allowed<template v-if="keptAllowances.length"> ({{ keptAllowances.join(', ') }})</template>.</template>
            <template v-else>This replaces the rule’s list of bypassing Apps. If other Apps are already listed there, add their slugs to the command too.</template>
          </p>
        </template>
        <p v-else class="mt-2 text-xs text-muted">Use the GitHub settings steps above. A CLI command is available only when Forge can read the controller App name and the complete existing bypass list.</p>
        <p class="mt-2 text-xs text-muted">If your rulesets already cover the same requirements, you can delete the classic rule instead.</p>
      </div>
      <div v-if="fix.rulesets.length" :id="(idPrefix || '') + 'bypass-rulesets'" class="rounded-lg border border-line bg-bg p-3">
        <h3 class="text-sm font-semibold text-white">Let Forge bypass {{ fix.rulesets.length > 1 ? 'the rulesets' : 'the ruleset' }}</h3>
        <p class="mt-1 text-xs text-muted">
          Edit <template v-for="(source, index) in fix.rulesets" :key="source.url">{{ index ? ', ' : '' }}<a :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline-offset-4 hover:underline">“{{ source.name }}” ↗</a></template>
          on GitHub and add the App <strong class="text-white">{{ appName }}</strong> as a “pull request” bypass actor: under “Bypass list”, choose “For pull requests only”, then save.
          <template v-if="downloadHref">The <a :href="downloadHref" :download="rulesetFilename(row)" class="text-sky-200 underline-offset-4 hover:underline" @click="$emit('download')">current review rules</a> include the controller bypass for reference.</template>
        </p>
        <UButton v-if="fix.rulesets.length === 1" :id="(idPrefix || '') + 'bypass-edit'" :to="fix.rulesets[0]!.url" target="_blank" rel="noopener noreferrer" size="xs" color="neutral" variant="outline" class="mt-2">Edit ruleset on GitHub ↗</UButton>
      </div>
      <p class="text-xs text-muted"><template v-if="failed.length">Also apply the requirements listed above. </template>After saving on GitHub, check again here.</p>
    </div>
  </section>

  <!-- Applying the review rules on GitHub. -->
  <section v-if="instructions && !displayReady && prepared && !fix" :id="(idPrefix || '') + 'steps'" class="mt-4 rounded-xl border border-line bg-bg p-4">
    <ol class="flex flex-col gap-4">
      <li v-if="!editUrl" :id="(idPrefix || '') + 'download-step'">
        <h3 class="text-sm font-semibold text-white">Download the review rules</h3>
        <p class="mt-0.5 text-xs text-muted">A ready-to-import file. No editing or administrator token needed.</p>
        <UButton
          :id="(idPrefix || '') + 'download'"
          :to="downloadHref"
          :download="rulesetFilename(row)"
          :disabled="!downloadHref"
          external
          size="xs"
          variant="outline"
          class="mt-2"
          @click="$emit('download')"
        >{{ downloaded ? 'Download again' : 'Download ruleset' }}</UButton>
      </li>
      <li>
        <h3 :id="(idPrefix || '') + 'import-title'" class="text-sm font-semibold text-white">{{ editUrl ? 'Update the existing ruleset' : 'Import on GitHub' }}</h3>
        <p :id="(idPrefix || '') + 'import-description'" class="mt-0.5 text-xs text-muted">
          {{ editUrl
            ? 'A Forge review ruleset already exists. Update its review settings using the requirements above.'
            : 'On GitHub, choose New ruleset → Import a ruleset. Select the file, review the settings and save.' }}
        </p>
        <UButton
          :id="(idPrefix || '') + 'github'"
          :to="editUrl || row.rules_url"
          target="_blank"
          rel="noopener noreferrer"
          size="xs"
          color="neutral"
          variant="outline"
          class="mt-2"
        >{{ editUrl ? 'Edit ruleset on GitHub ↗' : 'Open GitHub settings ↗' }}</UButton>
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
        <p v-if="bypass?.status === 'ready'" :id="(idPrefix || '') + 'bypass'" class="mt-2"><span aria-hidden="true" class="text-mint-200">✓</span> The Forge controller can merge approved work past these rules.</p>
        <p v-else-if="bypass?.status === 'missing'" :id="(idPrefix || '') + 'bypass'" class="mt-2"><strong class="text-white">The Forge controller is not a bypass actor.</strong> Add it as a “pull request” bypass actor, as described above.</p>
        <p v-else :id="(idPrefix || '') + 'bypass'" class="mt-2"><strong class="text-white">Bypass permissions need your review.</strong> GitHub may hide who can bypass a ruleset. Check the bypass list on GitHub; a successful check here does not certify that list.</p>
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

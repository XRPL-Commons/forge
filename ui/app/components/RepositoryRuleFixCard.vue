<script setup lang="ts">
import { bypassSourceLabel, classicBypassCommand, rulesetBypassCommand, type ProtectionSource } from '~/utils/repositories'
const props = defineProps<{ source: ProtectionSource; repo: string; branch: string; appId?: number; appSlug?: string | null; prefix: string; fixed: boolean }>()
const command = computed(() => props.source.kind === 'classic'
  ? classicBypassCommand(props.repo, props.branch, props.appSlug || '', props.source.bypass_allowances)
  : rulesetBypassCommand(props.repo, props.source, props.appId))
const tab = ref('command')
const items = computed(() => command.value ? [{ label: 'Command', value: 'command', slot: 'command' as const }, { label: 'Manual steps', value: 'manual', slot: 'manual' as const }] : [{ label: 'Manual steps', value: 'manual', slot: 'manual' as const }])
watch(command, value => { tab.value = value ? 'command' : 'manual' }, { immediate: true })
const app = computed(() => props.appSlug || (props.appId ? `ID ${props.appId}` : 'Forge controller'))
const copied = ref('Copy command')
const commandBlock = useTemplateRef<HTMLElement>('commandBlock')
async function copy() {
  try { await navigator.clipboard.writeText(command.value); copied.value = 'Copied' }
  catch {
    copied.value = 'Select and copy the command above'
    if (!commandBlock.value) return
    const range = document.createRange(); range.selectNodeContents(commandBlock.value)
    const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range)
  }
}
</script>
<template>
  <section data-rule-fix class="rounded-lg border border-line bg-bg p-3">
    <div class="flex items-start justify-between gap-3">
      <h3 class="text-sm font-semibold text-white">{{ bypassSourceLabel(source, branch) }}</h3>
      <UBadge v-if="fixed" color="success" variant="subtle">Fixed</UBadge>
    </div>
    <UTabs v-if="!fixed" v-model="tab" :items="items" variant="link" size="sm" class="mt-2" :aria-label="`Fix ${source.name}`">
      <template #command>
        <p class="text-xs text-muted">Run locally as a {{ source.source_type === 'Organization' ? 'organization' : 'repository' }} administrator{{ source.kind === 'ruleset' ? ' with gh and jq' : ' with gh' }}.</p>
        <pre ref="commandBlock" :id="prefix + 'bypass-command'" class="mt-2 rounded-lg border border-line bg-panel p-3 text-xs whitespace-pre-wrap break-all">{{ command }}</pre>
        <UButton :id="prefix + 'bypass-copy'" size="xs" variant="outline" class="mt-2" @click="copy">{{ copied }}</UButton>
      </template>
      <template #manual>
        <ol class="list-decimal space-y-1 pl-5 text-sm text-muted">
          <li>Open <a :id="prefix + 'bypass-edit'" :href="source.url" target="_blank" rel="noopener noreferrer" class="text-sky-200 underline">{{ source.kind === 'classic' ? 'Branch protection settings' : source.name }} ↗</a><template v-if="source.kind === 'classic'"> and edit the <strong>{{ branch }}</strong> rule</template>.</li>
          <li v-if="source.kind === 'classic'">Under “Require a pull request before merging”, select “Allow specified actors to bypass required pull requests”.</li>
          <li v-else>Open “Bypass list” and choose “Add bypass”.</li>
          <li>Add App <strong class="text-white">{{ app }}</strong><template v-if="source.kind === 'ruleset'"> and choose “For pull requests only”</template>.</li>
          <li>Save, then check again here.</li>
        </ol>
      </template>
    </UTabs>
  </section>
</template>

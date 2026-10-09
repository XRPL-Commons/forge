<script setup lang="ts">
import type { IssueEnvironment, IssueEnvironmentSelection } from '~/utils/issues'
const props = defineProps<{ environment: IssueEnvironment; busy: boolean; open: boolean; error: string }>()
const emit = defineEmits<{ 'update:open': [boolean]; save: [IssueEnvironmentSelection] }>()
const mode = ref<IssueEnvironmentSelection['mode']>('automatic')
const selected = ref<string[]>([])
const services = [{ label: 'MongoDB', value: 'mongodb' }, { label: 'PostgreSQL', value: 'postgres' }, { label: 'Redis', value: 'redis' }]
const names = (values: string[]) => values.map(value => services.find(s => s.value === value)?.label ?? value).join(', ')
const options = computed(() => [
  { label: 'Automatic', value: 'automatic' },
  { label: 'Standard — no databases', value: 'standard' },
  { label: 'Choose databases', value: 'full', disabled: !props.environment.full_available },
])
watch(() => props.open, open => {
  if (open) {
    mode.value = props.environment.override?.mode ?? 'automatic'
    selected.value = [...(props.environment.override?.services ?? props.environment.choice?.services ?? [])]
  }
})
function toggle(service: string, checked: boolean | 'indeterminate') {
  selected.value = checked === true ? [...new Set([...selected.value, service])] : selected.value.filter(value => value !== service)
}
</script>

<template>
  <section id="issue-environment" class="mt-4 rounded-xl border border-line bg-panel p-4" aria-labelledby="issue-environment-title">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 id="issue-environment-title" class="text-sm font-semibold text-white">Worker environment</h2>
      <UButton v-if="environment.editable && !open" color="neutral" variant="outline" size="sm" :disabled="busy" @click="emit('update:open', true)">Change environment</UButton>
    </div>
    <template v-if="environment.choice">
      <p class="mt-2 text-sm font-medium">{{ environment.choice.tier === 'full' ? names(environment.choice.services) : 'Standard environment' }}</p>
      <p class="mt-1 text-sm text-muted">{{ environment.choice.reason }}</p>
    </template>
    <p v-else class="mt-2 text-sm text-muted">{{ environment.override ? 'Next attempt: ' + (environment.override.mode === 'full' ? names(environment.override.services) : 'standard environment') + '.' : 'The supervisor will choose an environment for this issue when it assigns work.' }}</p>
    <p v-if="!environment.override && environment.choice?.automatic !== false" class="mt-1 text-xs text-muted">Automatic. Forge can adjust the databases if the worker reports a missing dependency.</p>
    <details v-if="environment.choice?.evidence?.length" class="mt-2 text-xs text-muted">
      <summary class="cursor-pointer text-sky-200">Repository evidence</summary>
      <ul class="mt-2 space-y-1"><li v-for="file in environment.choice.evidence" :key="file" class="break-words">{{ file }}</li></ul>
    </details>
    <form v-if="open" class="mt-3 space-y-3" @submit.prevent="emit('save', { mode, services: mode === 'full' ? selected : [] })">
      <p class="text-xs text-muted">Choose an optional override for this issue's next attempt. Select Automatic to return the choice to the supervisor.</p>
      <label for="issue-environment-mode" class="block text-sm">Environment selection</label>
      <USelect id="issue-environment-mode" v-model="mode" :items="options" class="w-full" :disabled="busy" />
      <fieldset v-if="mode === 'full'" class="space-y-2">
        <legend class="mb-2 text-sm">Databases</legend>
        <UCheckbox v-for="service in services" :key="service.value" :label="service.label" :model-value="selected.includes(service.value)" :disabled="busy" @update:model-value="toggle(service.value, $event)" />
      </fieldset>
      <p v-if="!environment.full_available" class="text-xs text-amber-200">Database workers are unavailable on this host.</p>
      <UAlert v-if="error" role="alert" color="error" variant="subtle" :description="error" />
      <DialogFooter>
        <template #secondary>
          <UButton color="neutral" variant="outline" :disabled="busy" @click="emit('update:open', false)">Cancel</UButton>
        </template>
        <template #primary>
          <UButton type="submit" :loading="busy" :disabled="busy || (mode === 'full' && (!selected.length || !environment.full_available))">Save for this issue</UButton>
        </template>
      </DialogFooter>
    </form>
  </section>
</template>

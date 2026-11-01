<script setup lang="ts">
// The four steps of repository setup (contracts §3.6). `stage` is the step the
// operator is on; stage 4 means every step is done and none is current.
const props = defineProps<{ stage: 0 | 1 | 2 | 3 | 4; activationAvailable: boolean; blocked?: boolean }>()

const steps = computed(() => ['Repository', 'App access', 'Merge policy', props.activationAvailable ? 'Enable' : 'Finish setup'])
</script>

<template>
  <ol id="repository-stage" class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" aria-label="Repository setup steps">
    <li
      v-for="(label, index) in steps"
      :key="label"
      :data-key="index"
      :aria-current="index === stage ? 'step' : undefined"
      class="flex items-center gap-2"
      :class="index === stage ? 'text-white' : index < stage ? 'text-mint-200' : 'text-muted'"
    >
      <span data-label>{{ index < stage ? '✓ ' : '' }}{{ label }}</span>
      <span v-if="index === stage && blocked" class="text-amber-200">Needs attention</span>
      <span v-if="index < steps.length - 1" aria-hidden="true" class="text-muted">›</span>
    </li>
  </ol>
</template>

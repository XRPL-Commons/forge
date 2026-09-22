<script setup lang="ts">
import { STEP_LABELS } from '~/utils/onboarding'

// The three setup steps (contracts §3.8): step 2 stays out of reach until
// all four roles are ok, step 3 until a repository is selected.
const props = defineProps<{ step: number; rolesOk: boolean; hasSelection: boolean }>()
const emit = defineEmits<{ select: [index: number] }>()

function locked(index: number) {
  return (index > 0 && !props.rolesOk) || (index === 2 && !props.hasSelection)
}
</script>

<template>
  <nav aria-label="Setup progress" class="grid gap-2 sm:pt-3">
    <button
      v-for="(label, index) in STEP_LABELS"
      :key="label"
      type="button"
      :data-step="index"
      :disabled="locked(index)"
      :aria-current="step === index ? 'step' : undefined"
      class="flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left disabled:cursor-not-allowed disabled:opacity-40"
      :class="step === index ? 'bg-panel text-white' : 'text-muted hover:text-white'"
      @click="emit('select', index)"
    >
      <span
        class="grid size-7 shrink-0 place-items-center rounded-full border text-xs"
        :class="step === index ? 'border-mint-200 text-mint-200' : 'border-line'"
      >{{ index + 1 }}</span>
      {{ label }}
    </button>
  </nav>
</template>

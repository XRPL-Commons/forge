<script setup lang="ts">
const props = withDefaults(defineProps<{
  label: string
  value: number | string
  detail?: string
  to?: string
  tone?: 'ok' | 'warn' | 'muted'
}>(), { detail: '', to: '', tone: 'ok' })

const valueClass = computed(() => props.tone === 'warn' ? 'text-amber-200' : props.tone === 'muted' ? 'text-muted' : 'text-white')
</script>

<template>
  <component
    :is="to ? 'NuxtLink' : 'div'"
    :to="to || undefined"
    class="block rounded-xl border border-line bg-panel px-4 py-3 no-underline"
    :class="to ? 'hover:border-mint-200/60' : ''"
  >
    <div class="text-xs uppercase tracking-wide text-muted">{{ label }}</div>
    <div class="mt-1 text-2xl font-semibold tabular-nums" :class="valueClass">{{ value }}</div>
    <div class="mt-0.5 truncate text-xs text-muted" :title="detail">{{ detail }}</div>
  </component>
</template>

<script setup lang="ts">
defineProps<{ title: string; summary: string; attention?: boolean }>()
const emit = defineEmits<{ 'update:open': [open: boolean] }>()
const loaded = ref(false)
function toggle(event: Event) {
  const open = (event.target as HTMLDetailsElement).open
  if (open) loaded.value = true
  emit('update:open', open)
}
</script>
<template>
  <details class="rounded-xl border border-line bg-panel p-4" @toggle="toggle">
    <summary class="cursor-pointer text-sm font-medium text-white">
      {{ title }}<span class="ml-2 font-normal" :class="attention ? 'text-amber-200' : 'text-muted'">{{ summary }}</span>
    </summary>
    <div v-if="loaded" class="mt-4"><slot /></div>
  </details>
</template>

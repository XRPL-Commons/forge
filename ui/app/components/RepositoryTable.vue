<script setup lang="ts">
import type { RepositoryRowModel } from '~/utils/repositories'

defineProps<{ rows: RepositoryRowModel[]; busy?: boolean }>()
const emit = defineEmits<{ toggle: [row: RepositoryRowModel] }>()

const columns = [{ accessorKey: 'name', header: 'Repository', id: 'name' }]

function switchLabel(row: RepositoryRowModel) {
  if (row.action === 'continue') return `Continue setup for ${row.name}`
  return row.action === 'disable' ? `Disable automation for ${row.name}` : `Enable automation for ${row.name}`
}
</script>

<template>
  <UTable
    :data="rows"
    :columns="columns"
    :ui="{ tr: 'transition-colors hover:bg-white/5', td: 'p-3 align-middle whitespace-normal', th: 'px-3 py-2 text-xs text-muted font-medium' }"
    empty="No matching repositories yet."
    aria-label="Repository automation"
  >
    <template #name-cell="{ row }">
      <div :data-key="row.original.id" class="flex items-center gap-3">
        <!-- The switch reflects the server's state and never flips on its own:
             turning it on opens the enable flow, turning it off asks first. -->
        <USwitch
          :model-value="row.original.action === 'disable'"
          :disabled="busy"
          :aria-label="switchLabel(row.original)"
          :color="row.original.state.icon === 'attention' ? 'warning' : 'primary'"
          size="md"
          @update:model-value="emit('toggle', row.original)"
        />
        <div class="min-w-0">
          <NuxtLink :to="row.original.to" class="break-all font-medium text-white underline-offset-4 hover:underline">{{ row.original.name }}</NuxtLink>
          <p class="mt-0.5 text-xs text-muted">{{ row.original.state.label }}</p>
          <p v-if="row.original.note" class="mt-0.5 text-xs text-amber-200"><NuxtLink :to="row.original.to" class="underline underline-offset-4">Resolve</NuxtLink></p>
        </div>
      </div>
    </template>
  </UTable>
</template>

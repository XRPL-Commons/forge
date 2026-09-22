<script setup lang="ts">
import type { RepositoryRowModel } from '~/utils/repositories'

defineProps<{ rows: RepositoryRowModel[]; busy?: boolean }>()
const emit = defineEmits<{ disable: [id: number] }>()

const columns = [
  { accessorKey: 'name', header: 'Repository', id: 'name' },
  { id: 'automation', header: 'Automation' },
]

const STATE_CLASS = { attention: 'text-amber-200', enabled: 'text-mint-200', off: 'text-muted' } as const
</script>

<template>
  <UTable
    :data="rows"
    :columns="columns"
    :ui="{ td: 'p-3 align-middle whitespace-normal', th: 'px-3 py-2 text-xs text-muted font-medium' }"
    empty="No matching repositories yet."
    aria-label="Repository automation"
  >
    <template #name-cell="{ row }">
      <div :data-key="row.original.id">
        <div class="flex items-center gap-2">
          <span role="img" :aria-label="row.original.state.label" :title="row.original.state.label" :class="STATE_CLASS[row.original.state.icon]">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="h-5 w-5" aria-hidden="true" focusable="false">
              <circle cx="12" cy="12" r="9" />
              <path v-if="row.original.state.icon === 'attention'" d="M9 8v8m6-8v8" />
              <path v-else-if="row.original.state.icon === 'enabled'" d="m7.5 12 3 3 6-6" />
            </svg>
          </span>
          <NuxtLink :to="row.original.to" class="break-all font-medium text-white underline-offset-4 hover:underline">{{ row.original.name }}</NuxtLink>
        </div>
        <p v-if="row.original.note" class="mt-1 text-xs text-amber-200">{{ row.original.note }}</p>
      </div>
    </template>

    <template #automation-cell="{ row }">
      <div class="flex items-center justify-end gap-3">
        <NuxtLink v-if="row.original.action === 'continue'" to="/onboarding" class="text-sm text-sky-200 underline-offset-4 hover:underline">Continue setup →</NuxtLink>
        <template v-else-if="row.original.action === 'disable'">
          <NuxtLink v-if="row.original.resolve" :to="row.original.resolve" class="text-sm text-sky-200 underline-offset-4 hover:underline">Resolve →</NuxtLink>
          <UButton size="xs" color="neutral" variant="ghost" :disabled="busy" @click="emit('disable', row.original.id)">Disable</UButton>
        </template>
        <UButton v-else :to="row.original.to" size="xs" color="primary" variant="outline">Set up</UButton>
      </div>
    </template>
  </UTable>
</template>

<script setup lang="ts">
import type { AccessRowModel } from '~/utils/repositories'
defineProps<{ connections: AccessRowModel[]; accessDescription: string; accessNote: string }>()
</script>
<template>
  <section id="app-access" class="mt-4 rounded-xl border border-line bg-panel p-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 class="text-sm font-semibold text-white">Connect the Forge Apps</h2>
    </div>
    <p class="mt-1 text-xs text-muted">{{ accessDescription }}</p>
    <p v-if="accessNote" id="access-result" role="status" class="mt-1 text-xs text-sky-200">{{ accessNote }}</p>
    <ul id="app-connections" class="mt-3 flex flex-col gap-2">
      <li v-for="connection in connections" :key="connection.role" :data-key="connection.role" class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2 text-xs">
        <div>
          <strong class="text-sm font-medium text-white">{{ connection.label }}</strong>
          <p v-if="!connection.ready" class="text-muted">{{ connection.message }}</p>
        </div>
        <span v-if="connection.ready" class="text-mint-200">Connected ✓</span>
        <NuxtLink
          v-else
          :to="connection.action.href"
          :target="connection.action.external ? '_blank' : undefined"
          :rel="connection.action.external ? 'noopener noreferrer' : undefined"
          :external="connection.action.external"
          class="text-sky-200 underline-offset-4 hover:underline"
        >{{ connection.action.label }}</NuxtLink>
      </li>
    </ul>
  </section>

</template>

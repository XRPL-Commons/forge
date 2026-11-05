<script setup lang="ts">
useHead({ title: 'Connections' })
const connections = [
  { to: '/settings/harnesses', title: 'Coding accounts', description: 'Connect Codex, Claude Code or another coding provider.' },
  { to: '/settings/github', title: 'GitHub', description: 'Choose which repositories Forge can access.' },
  { to: '/settings/agents', title: 'External agents', description: 'Manage agents connected to this factory.' },
]
</script>
<template>
  <div class="max-w-3xl">
    <p class="text-xs uppercase tracking-wide text-muted">Settings</p>
    <h1 class="text-3xl font-semibold text-white">Connections</h1>
    <div class="mt-5 divide-y divide-line rounded-xl border border-line bg-panel">
      <NuxtLink v-for="connection in connections" :key="connection.to" :to="connection.to" class="flex items-center justify-between gap-4 p-5 hover:bg-white/5">
        <div><h2 class="font-medium text-white">{{ connection.title }}</h2><p class="mt-1 text-sm text-muted">{{ connection.description }}</p></div>
        <span aria-hidden="true">→</span>
      </NuxtLink>
    </div>
  </div>
</template>

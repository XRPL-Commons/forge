<script setup lang="ts">
useHead({ title: 'Repository settings' })
const route = useRoute()
const id = computed(() => Number(route.params.id))
if (!Number.isSafeInteger(id.value) || id.value <= 0) await navigateTo('/settings/repositories', { replace: true })
</script>
<template><RepositoryDetails :key="id" :repo-id="id" /></template>

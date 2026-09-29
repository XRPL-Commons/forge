<script setup lang="ts">
const route = useRoute()
const id = Number(route.query.repo_id)
await navigateTo(Number.isSafeInteger(id) && id > 0 ? `/settings/repositories/${id}` : '/settings/repositories?add=1', { replace: true, redirectCode: 302 })
</script>
<template><span /></template>

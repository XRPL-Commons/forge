<script setup lang="ts">
const props = defineProps<{ id: string; text: string }>()
const { data: rendered } = await useAsyncData(`conversation-markdown-${props.id}`, () =>
  $fetch<{ html: string }>('/_ui/markdown', { method: 'POST', body: { text: props.text } }))
</script>
<template><MarkdownBody v-if="rendered" :html="rendered.html" /><p v-else class="whitespace-pre-wrap break-words text-sm">{{ text }}</p></template>
